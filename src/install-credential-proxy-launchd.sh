#!/bin/bash
# Install / uninstall the launchd-supervised credential-proxy job.
#
# Role: keeps quota-tracker's credential-proxy alive at port 7846 with
# automatic restart on crash + ThrottleInterval to prevent the EADDRINUSE
# crash-loop described in issue #1086.
#
# TCC safety: launchd's bash has no Documents/Desktop access grant, so the
# job must not read anything that resolves into the repo checkout (commonly
# ~/Documents/... or ~/Desktop/... — including through the
# ~/.claude/skills/quota-tracker/scripts symlink). Install therefore VENDORS
# the job into $LAUNCHD_DIR (outside TCC scope): the proxy is esbuild-bundled
# to a single node-runnable .mjs and the wrapper is copied next to it. After
# pulling changes to credential-proxy.ts, re-run install to re-vendor.
#
# Usage:
#   bash src/install-credential-proxy-launchd.sh             # install
#   bash src/install-credential-proxy-launchd.sh --uninstall # remove
#   bash src/install-credential-proxy-launchd.sh --status    # print job state
#
# Idempotent: re-running install bootouts the existing job, re-vendors, and
# reloads so a git pull that changes the template or proxy is picked up.

set -e

LABEL="com.sutando.credential-proxy"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/src/launchd/$LABEL.plist"
WRAPPER_SRC="$REPO/src/launchd/credential-proxy-wrapper.sh"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
LAUNCHD_DIR="$HOME/.sutando/launchd"
PROXY_SRC="$HOME/.claude/skills/quota-tracker/scripts/credential-proxy.ts"

if [ -n "${SUTANDO_WORKSPACE:-}" ]; then
  WORKSPACE="${SUTANDO_WORKSPACE/#\~/$HOME}"
else
  WORKSPACE="$HOME/.sutando/workspace"
fi

resolve_brew_bin() {
    if [ -d /opt/homebrew/bin ]; then
        echo /opt/homebrew/bin
    elif [ -d /usr/local/bin ]; then
        echo /usr/local/bin
    else
        echo /usr/bin
    fi
}

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

cmd="${1:-install}"

case "$cmd" in
    install)
        if [ ! -f "$TEMPLATE" ]; then
            echo "ERROR: template not found: $TEMPLATE" >&2
            exit 1
        fi
        if [ ! -f "$PROXY_SRC" ]; then
            echo "ERROR: quota-tracker skill not found at ~/.claude/skills/quota-tracker/" >&2
            echo "  Install it first — credential-proxy.ts is the proxy target." >&2
            exit 1
        fi
        ESBUILD="$REPO/node_modules/.bin/esbuild"
        if [ ! -x "$ESBUILD" ]; then
            echo "ERROR: esbuild not found at $ESBUILD — run npm install in the repo first." >&2
            exit 1
        fi
        BREW_BIN="$(resolve_brew_bin)"
        echo "Installing $LABEL"
        echo "  repo:       $REPO"
        echo "  workspace:  $WORKSPACE"
        echo "  vendor dir: $LAUNCHD_DIR"
        echo "  brew bin:   $BREW_BIN"
        mkdir -p "$HOME/Library/LaunchAgents"
        mkdir -p "$WORKSPACE/logs"
        mkdir -p "$LAUNCHD_DIR"
        # Vendor: single node-runnable bundle (proxy + its repo-local imports),
        # so the launchd job never reads through the repo checkout at runtime.
        "$ESBUILD" "$PROXY_SRC" --bundle --platform=node --format=esm \
            --outfile="$LAUNCHD_DIR/credential-proxy.mjs" --log-level=warning
        cp "$WRAPPER_SRC" "$LAUNCHD_DIR/credential-proxy-wrapper.sh"
        chmod +x "$LAUNCHD_DIR/credential-proxy-wrapper.sh"
        sed \
            -e "s|__LAUNCHD_DIR__|$LAUNCHD_DIR|g" \
            -e "s|__WORKSPACE__|$WORKSPACE|g" \
            -e "s|__BREW_BIN__|$BREW_BIN|g" \
            -e "s|__HOME__|$HOME|g" \
            "$TEMPLATE" > "$DEST"
        bootout_if_loaded
        launchctl bootstrap "$DOMAIN" "$DEST"
        echo "  Loaded via $SERVICE"
        echo
        echo "credential-proxy is now launchd-managed (KeepAlive, ThrottleInterval=10s)."
        echo "  • View status:  bash $0 --status"
        echo "  • Uninstall:    bash $0 --uninstall"
        echo "  • Logs:         $WORKSPACE/logs/credential-proxy.log"
        echo "  • After changing credential-proxy.ts: re-run install to re-vendor."
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
        rm -f "$LAUNCHD_DIR/credential-proxy.mjs" "$LAUNCHD_DIR/credential-proxy-wrapper.sh"
        rmdir "$LAUNCHD_DIR" 2>/dev/null || true
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
