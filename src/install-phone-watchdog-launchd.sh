#!/bin/bash
# Install / uninstall the launchd-supervised phone-stack watchdog.
#
# Role: supervise the phone stack (conversation-server :3100 + reserved-domain
# ngrok tunnel). startup.sh starts them fire-and-forget with no supervision, so a
# host sleep/reboot/process death leaves the Twilio number answering Twilio's
# generic "application error" until someone notices. This job curls the PUBLIC
# webhook /health every 120s and re-runs the launcher when it is down.
#
# What this does:
#   - Renders src/launchd/com.sutando.phone-watchdog.plist with absolute paths
#     and writes it to ~/Library/LaunchAgents/com.sutando.phone-watchdog.plist
#   - Loads it via `launchctl bootstrap gui/$UID` (the modern Sequoia idiom).
#   - Result: macOS runs `bash src/phone-watchdog.sh` every 120s, independent of
#     any Sutando session.
#
# Usage:
#   bash src/install-phone-watchdog-launchd.sh             # install (idempotent)
#   bash src/install-phone-watchdog-launchd.sh --uninstall # remove (idempotent)
#   bash src/install-phone-watchdog-launchd.sh --status    # print job state
#
# Idempotent: re-running install bootouts the existing job before bootstrapping
# the new one, so a `git pull` that updates the template is picked up by
# re-running this script.

set -e

LABEL="com.sutando.phone-watchdog"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/src/launchd/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"

# Resolve runtime workspace via the shared post-M0 helper (single source at
# src/workspace_resolve.sh). The job writes its log under $WORKSPACE/logs/.
__HELPER="$REPO/src/workspace_resolve.sh"
[ -f "$__HELPER" ] || __HELPER="$(cd "$(dirname "$0")" && pwd)/workspace_resolve.sh"
if [ -f "$__HELPER" ]; then
  # shellcheck source=workspace_resolve.sh
  source "$__HELPER"
  resolve_workspace_or_die
elif [ -n "${SUTANDO_WORKSPACE:-}" ]; then
  WORKSPACE="${SUTANDO_WORKSPACE/#\~/$HOME}"
else
  echo "${0##*/}: cannot resolve workspace — workspace_resolve.sh not found and \$SUTANDO_WORKSPACE not set." >&2
  exit 1
fi
unset __HELPER

cmd="${1:-install}"

bootout_if_loaded() {
    if launchctl print "$SERVICE" >/dev/null 2>&1; then
        echo "  Existing job found, removing first..."
        launchctl bootout "$SERVICE" 2>/dev/null || true
        for _ in $(seq 1 10); do
            launchctl print "$SERVICE" >/dev/null 2>&1 || break
            sleep 0.3
        done
    fi
}

resolve_homebrew_bin() {
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
        BREW_BIN="$(resolve_homebrew_bin)"
        echo "Installing $LABEL"
        echo "  repo: $REPO"
        echo "  brew: $BREW_BIN"
        mkdir -p "$HOME/Library/LaunchAgents"
        mkdir -p "$WORKSPACE/logs"
        sed \
            -e "s|__REPO__|$REPO|g" \
            -e "s|__WORKSPACE__|$WORKSPACE|g" \
            -e "s|__HOMEBREW_BIN__|$BREW_BIN|g" \
            "$TEMPLATE" > "$DEST"
        bootout_if_loaded
        launchctl bootstrap "$DOMAIN" "$DEST"
        echo "  Loaded via $SERVICE"

        # Self-test: prove the watchdog script runs under this shell without
        # touching a real stack (DRY_RUN prints the recovery action instead of
        # running it). This does not exercise the launchd TCC path, only that the
        # script itself is invocable.
        if DRY_RUN=1 bash "$REPO/src/phone-watchdog.sh" >/dev/null 2>&1; then
            echo "  Self-test: OK — phone-watchdog.sh runs (DRY_RUN)."
        else
            echo "  Self-test: FAILED — phone-watchdog.sh errored under DRY_RUN." >&2
            exit 1
        fi
        echo
        echo "Sutando — phone-watchdog is now running every 120s."
        echo "  • Curls the PUBLIC webhook /health; re-runs the launcher if down"
        echo "  • View status:  bash $0 --status"
        echo "  • Uninstall:    bash $0 --uninstall"
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
