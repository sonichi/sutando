#!/bin/bash
# Install / uninstall the launchd-supervised health-check FALLBACK job.
#
# Role: OS-level safety net for "all of Sutando is dead." Sutando.app's
# in-process Timer (PR #613) is the primary 30min health-check while the
# menu-bar app is alive. This job is the redundant supervisor that keeps
# running even when Sutando.app exits / crashes / signs out — closing the
# circular-dependency gap that motivated PR #616.
#
# What this does:
#   - Renders src/launchd/com.sutando.health-check-fallback.plist with
#     absolute paths and writes it to
#     ~/Library/LaunchAgents/com.sutando.health-check-fallback.plist
#   - Loads it via `launchctl bootstrap gui/$UID` (the modern Sequoia idiom).
#   - Result: macOS runs `python3 src/health-check.py --emit-task
#     --notify-on-fail --quiet` every 5min, independent of any other Sutando
#     process. Failures surface as tasks (for the agent to act on) AND as
#     macOS notifications (so the human sees them even if all of Sutando is
#     dead).
#
# What the user sees first time they install:
#   - One macOS "Background Item Added" notification banner (Apple's own UX,
#     not Sutando's). Dismissable.
#   - A new "Sutando — Health Check" entry in System Settings → General →
#     Login Items → "Allow in the Background" with a toggle. Disable any
#     time without breaking Sutando.
#
# Strictly opt-in: not called by startup.sh. Run this script when you want
# OS-supervised health detection.
#
# Usage:
#   bash src/install-health-check-launchd.sh             # install (idempotent)
#   bash src/install-health-check-launchd.sh --uninstall # remove (idempotent)
#   bash src/install-health-check-launchd.sh --status    # print job state
#
# Idempotent: re-running install bootouts the existing job before
# bootstrapping the new one, so a `git pull` that updates the template is
# picked up by re-running this script.

set -e

LABEL="com.sutando.health-check-fallback"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/src/launchd/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"

cmd="${1:-install}"

bootout_if_loaded() {
    if launchctl print "$SERVICE" >/dev/null 2>&1; then
        echo "  Existing job found, removing first..."
        launchctl bootout "$SERVICE" 2>/dev/null || true
        # bootout is async — wait for the service to actually disappear so
        # the subsequent bootstrap doesn't race.
        for _ in $(seq 1 10); do
            launchctl print "$SERVICE" >/dev/null 2>&1 || break
            sleep 0.3
        done
    fi
}

resolve_python() {
    # Prefer Homebrew python3 — system /usr/bin/python3 is 3.9 on older
    # Macs and health-check.py uses 3.10+ syntax (per agent-api.py:115
    # comment).
    if [ -x /opt/homebrew/bin/python3 ]; then
        echo /opt/homebrew/bin/python3
    elif [ -x /usr/local/bin/python3 ]; then
        echo /usr/local/bin/python3
    elif command -v python3 >/dev/null 2>&1; then
        command -v python3
    else
        echo "ERROR: no python3 found" >&2
        exit 1
    fi
}

resolve_homebrew_bin() {
    # Apple Silicon vs Intel — both prefixes work; pick whichever exists.
    if [ -d /opt/homebrew/bin ]; then
        echo /opt/homebrew/bin
    elif [ -d /usr/local/bin ]; then
        echo /usr/local/bin
    else
        echo /usr/bin
    fi
}

case "$cmd" in
    install)
        if [ ! -f "$TEMPLATE" ]; then
            echo "ERROR: template not found: $TEMPLATE" >&2
            exit 1
        fi
        PYTHON_BIN="$(resolve_python)"
        BREW_BIN="$(resolve_homebrew_bin)"
        echo "Installing $LABEL"
        echo "  repo:    $REPO"
        echo "  python:  $PYTHON_BIN"
        echo "  brew:    $BREW_BIN"
        mkdir -p "$HOME/Library/LaunchAgents"
        mkdir -p "$REPO/logs"
        # Render the template. Use a delimiter unlikely to appear in paths.
        sed \
            -e "s|__REPO__|$REPO|g" \
            -e "s|__PYTHON__|$PYTHON_BIN|g" \
            -e "s|__HOMEBREW_BIN__|$BREW_BIN|g" \
            "$TEMPLATE" > "$DEST"
        bootout_if_loaded
        launchctl bootstrap "$DOMAIN" "$DEST"
        echo "  Loaded via $SERVICE"
        echo
        echo "Sutando — Health Check (fallback) is now running every 5min."
        echo "  • Failures fire macOS notifications + write tasks/task-health-*.txt"
        echo "  • View status:  bash $0 --status"
        echo "  • Uninstall:    bash $0 --uninstall"
        echo "  • Disable temporarily: System Settings → General → Login Items"
        echo "    → 'Allow in the Background' → toggle off Sutando — Health Check"
        ;;
    --uninstall|uninstall)
        echo "Uninstalling $LABEL"
        bootout_if_loaded
        if [ -f "$DEST" ]; then
            rm "$DEST"
            echo "  Removed $DEST"
        else
            echo "  (no plist on disk; nothing to remove)"
        fi
        echo "Done."
        ;;
    --status|status)
        echo "Service: $SERVICE"
        if launchctl print "$SERVICE" >/dev/null 2>&1; then
            launchctl print "$SERVICE" | grep -E '^\s+(state|pid|last exit code|runs|path)' || true
        else
            echo "  (not loaded)"
        fi
        ;;
    *)
        echo "Usage: $0 [install|--uninstall|--status]" >&2
        exit 2
        ;;
esac
