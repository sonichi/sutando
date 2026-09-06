#!/bin/bash
# Install / uninstall the launchd phone-stack watchdog. See README.
# Install bootouts first, so re-running picks up a pulled template.

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

# Ask brew where it lives instead of naming its prefix: the Apple-Silicon and
# Intel prefixes differ, and a literal for either is a hardcoded host path.
resolve_homebrew_bin() {
    local prefix
    if prefix="$(brew --prefix 2>/dev/null)" && [ -d "$prefix/bin" ]; then
        echo "$prefix/bin"
        return
    fi
    # No brew: fall back to the directory of a tool launchd will need anyway.
    prefix="$(command -v bash 2>/dev/null)"
    [ -n "$prefix" ] && { dirname "$prefix"; return; }
    echo /usr/bin
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

        # Proves the script is invocable under this shell, not that launchd's
        # TCC path works.
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
