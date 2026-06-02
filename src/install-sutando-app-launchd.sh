#!/bin/bash
# Install / uninstall / check the launchd-supervised Sutando.app job.
#
# Role: OS-level crash-recovery supervisor for the Sutando menu-bar app.
# Without this, a crash requires a manual `open Sutando.app`; with it,
# launchd restarts the app within ~10s of any non-zero exit.
#
# Restart policy:
#   KeepAlive { Crashed: true, SuccessfulExit: false }
#   → restarts on crashes / OOM
#   → does NOT restart on clean user quit (exit 0 from menu bar)
#   ThrottleInterval=10 caps at one restart per 10s.
#
# TCC note: this job runs the binary inside the .app bundle. macOS resolves
# TCC (Accessibility, Screen Recording) by bundle ID — grants survive
# launchd-managed restarts PROVIDED the code signing identity is unchanged.
# If you re-sign or rebuild the binary, you must re-grant TCC manually.
# See feedback_codesign_change_orphans_tcc_grant.md.
#
# Usage:
#   bash src/install-sutando-app-launchd.sh              # install (idempotent)
#   bash src/install-sutando-app-launchd.sh --uninstall  # remove (idempotent)
#   bash src/install-sutando-app-launchd.sh --status     # print job state
#
# Idempotent: re-running install bootouts the existing job and re-loads
# with fresh paths. Safe to re-run after `git pull` or path changes.

set -e

LABEL="com.sutando.menubar"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/src/launchd/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"

# Resolve runtime workspace via the shared post-M0 helper (PR #1395, single
# source at src/workspace_resolve.sh). Defensive fallback for non-checkout
# installs where the helper file isn't reachable.
# Helper resolution: prefer $REPO/src/, fall back to script-sibling (cross-
# checkout safety — see init.sh comment).
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

APP_BINARY="$REPO/src/Sutando/Sutando.app/Contents/MacOS/Sutando"

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

case "$cmd" in
    install)
        if [ ! -f "$TEMPLATE" ]; then
            echo "ERROR: template not found: $TEMPLATE" >&2
            exit 1
        fi
        if [ ! -f "$APP_BINARY" ]; then
            echo "ERROR: Sutando binary not found: $APP_BINARY" >&2
            echo "  Build with: swiftc src/Sutando/main.swift -o $APP_BINARY" >&2
            exit 1
        fi
        echo "Installing $LABEL"
        echo "  repo:    $REPO"
        echo "  binary:  $APP_BINARY"
        echo "  workspace: $WORKSPACE"
        mkdir -p "$HOME/Library/LaunchAgents"
        mkdir -p "$WORKSPACE/logs"
        # Render the template.
        sed \
            -e "s|__REPO__|$REPO|g" \
            -e "s|__WORKSPACE__|$WORKSPACE|g" \
            -e "s|__HOME__|$HOME|g" \
            "$TEMPLATE" > "$DEST"
        bootout_if_loaded
        launchctl bootstrap "$DOMAIN" "$DEST"
        # If Sutando.app was already running before install, the launchd-spawned
        # instance exits cleanly via singleton-detection; kickstart forces
        # launchd to take ownership so KeepAlive applies. Idempotent.
        launchctl kickstart "$SERVICE" >/dev/null 2>&1 || true
        echo "  Loaded via $SERVICE"
        echo
        echo "Sutando.app is now launchd-supervised."
        echo "  • Crashes auto-restart within ~10s"
        echo "  • Clean quit (menu bar) is NOT auto-restarted"
        echo "  • Logs: $WORKSPACE/logs/sutando-app-stdout.log"
        echo "  • View status:  bash $0 --status"
        echo "  • Uninstall:    bash $0 --uninstall"
        echo
        echo "IMPORTANT — TCC verification (run once after install):"
        echo "  1. kill \$(pgrep -x Sutando) — force a crash restart"
        echo "  2. Wait ~10s for launchd to restart the app"
        echo "  3. Test Ctrl+C / Ctrl+V hotkeys — if broken, re-grant"
        echo "     System Settings → Privacy & Security → Accessibility"
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
        echo "  Note: this also terminated the running Sutando.app. Run \`open Sutando.app\` to relaunch unmanaged."
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
