#!/bin/bash
# Install / uninstall the launchd-supervised credential-proxy job.
#
# Role: Replace the bare background process in startup.sh with an
# OS-supervised daemon. The launchd job:
#   - Starts on user login (RunAtLoad=true)
#   - Restarts automatically on crash (KeepAlive=true)
#   - Throttles restarts to 10s (ThrottleInterval) — prevents EADDRINUSE
#     crash-loops (observed: 3500+ restarts in 30min when a stale process
#     held port 7846)
#   - Writes all output to $WORKSPACE/logs/credential-proxy-launchd.log
#
# After install, startup.sh's "if ! lsof -i :7846" block still works as
# before — it just finds the port already up (launchd started it at login)
# and skips the manual background spawn. The ANTHROPIC_BASE_URL export
# in startup.sh is unchanged.
#
# Strictly opt-in: startup.sh does NOT call this. Run it once to enable
# launchd supervision, then leave it — it persists across reboots.
#
# Usage:
#   bash src/install-credential-proxy-launchd.sh             # install (idempotent)
#   bash src/install-credential-proxy-launchd.sh --uninstall # remove (idempotent)
#   bash src/install-credential-proxy-launchd.sh --status    # print job state
#
# Idempotent: re-running install bootouts the existing job before
# bootstrapping the new one, so a template edit is picked up by re-running.

set -e

LABEL="com.sutando.credential-proxy"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/src/launchd/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"

# Workspace resolution — same shape as startup.sh and workspace_default.py.
if [ -n "${SUTANDO_WORKSPACE:-}" ]; then
  WORKSPACE="${SUTANDO_WORKSPACE/#\~/$HOME}"
else
  WORKSPACE="$HOME/.sutando/workspace"
fi

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

resolve_npx() {
    # Prefer Homebrew npx, then nvm, then PATH.
    if [ -x /opt/homebrew/bin/npx ]; then
        echo /opt/homebrew/bin/npx
    elif [ -x /usr/local/bin/npx ]; then
        echo /usr/local/bin/npx
    elif command -v npx >/dev/null 2>&1; then
        command -v npx
    else
        echo "ERROR: npx not found — install Node.js first" >&2
        exit 1
    fi
}

resolve_node_bin() {
    # Directory containing `node` — injected into PATH in the plist so
    # tsx and any spawned subprocesses can find it.
    local npx_path
    npx_path="$(resolve_npx)"
    dirname "$npx_path"
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

resolve_credential_proxy() {
    # canonical path: skill dir under ~/.claude
    local skill_path="$HOME/.claude/skills/quota-tracker/scripts/credential-proxy.ts"
    if [ -f "$skill_path" ]; then
        echo "$skill_path"
        return
    fi
    # fallback: symlink in project skills/
    local proj_path="$REPO/skills/quota-tracker/scripts/credential-proxy.ts"
    if [ -f "$proj_path" ]; then
        echo "$proj_path"
        return
    fi
    echo "ERROR: credential-proxy.ts not found at $skill_path or $proj_path" >&2
    exit 1
}

case "$cmd" in
    install)
        if [ ! -f "$TEMPLATE" ]; then
            echo "ERROR: template not found: $TEMPLATE" >&2
            exit 1
        fi

        NPX_BIN="$(resolve_npx)"
        NODE_BIN="$(resolve_node_bin)"
        BREW_BIN="$(resolve_homebrew_bin)"
        PROXY_TS="$(resolve_credential_proxy)"

        echo "Installing $LABEL"
        echo "  repo:       $REPO"
        echo "  workspace:  $WORKSPACE"
        echo "  npx:        $NPX_BIN"
        echo "  node bin:   $NODE_BIN"
        echo "  proxy:      $PROXY_TS"

        mkdir -p "$HOME/Library/LaunchAgents"
        mkdir -p "$WORKSPACE/logs"

        sed \
            -e "s|__REPO__|$REPO|g" \
            -e "s|__WORKSPACE__|$WORKSPACE|g" \
            -e "s|__NPX__|$NPX_BIN|g" \
            -e "s|__NODE_BIN__|$NODE_BIN|g" \
            -e "s|__HOMEBREW_BIN__|$BREW_BIN|g" \
            -e "s|__CREDENTIAL_PROXY__|$PROXY_TS|g" \
            -e "s|__HOME__|$HOME|g" \
            "$TEMPLATE" > "$DEST"

        bootout_if_loaded
        launchctl bootstrap "$DOMAIN" "$DEST"
        echo "  Loaded via $SERVICE"
        echo
        echo "Credential proxy is now launchd-supervised (KeepAlive + 10s throttle)."
        echo "  • Logs:      $WORKSPACE/logs/credential-proxy-launchd.log"
        echo "  • View status:  bash $0 --status"
        echo "  • Uninstall:    bash $0 --uninstall"
        echo "  • startup.sh still works as before — launchd starts proxy at login"
        echo "    so startup.sh finds port 7846 already up and skips the spawn."
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
