#!/bin/bash
# Install / uninstall the launchd-supervised credential-proxy job.
#
# Role: keeps quota-tracker's credential-proxy alive at port 7846 with
# automatic restart on crash + ThrottleInterval to prevent the EADDRINUSE
# crash-loop described in issue #1086.
#
# Usage:
#   bash src/install-credential-proxy-launchd.sh             # install
#   bash src/install-credential-proxy-launchd.sh --uninstall # remove
#   bash src/install-credential-proxy-launchd.sh --status    # print job state
#
# Idempotent: re-running install bootouts the existing job and reloads so a
# git pull that changes the template is picked up.

set -e

LABEL="com.sutando.credential-proxy"
# Logical `cd` (no -P, no realpath) is load-bearing: tests/credential-proxy-bundled-install.test.sh
# runs the installer from a staged repo whose src/ is a SYMLINK to the real src/, and relies on
# `$STAGE/src/..` resolving to $STAGE (not the symlink target) so the test only ever touches a
# scratch dist/. If you change this to `cd -P`/`realpath`, that test will silently start writing
# the real dist/credential-proxy.js — a production file on a bundled host. Keep it logical.
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
else
  echo "${0##*/}: cannot resolve workspace — workspace_resolve.sh not found. v0.8 contract requires the helper; \$SUTANDO_WORKSPACE is no longer honored." >&2
  exit 1
fi
unset __HELPER

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
        # Validate the target the WRAPPER will actually use. In bundled mode the
        # wrapper execs dist/credential-proxy.js and never reads the TS source
        # (credential-proxy-wrapper.sh), so gating on the TS source there would
        # reject a correctly-packaged host: a pristine bundled install ships dist
        # only, has no quota-tracker skill dir, and would end up with no proxy at
        # all. Detect bundled mode exactly as the wrapper does.
        _I_APP_NODE_DIR="$(bash "$REPO/scripts/sutando-config.sh" app-node-dir)"
        _I_ENGINE_ROOT="${_I_APP_NODE_DIR%/node/bin}"; _I_ENGINE_ROOT="${_I_ENGINE_ROOT%/runtime}"
        _I_BUNDLED=0
        if [ -n "${SUTANDO_NODE:-}" ]; then
            _I_BUNDLED=1
        elif [ -x "$_I_APP_NODE_DIR/node" ] && [ "${REPO#"$_I_ENGINE_ROOT"/}" != "$REPO" ]; then
            _I_BUNDLED=1
        fi
        if [ "$_I_BUNDLED" = "1" ]; then
            _PROXY_SCRIPT="$REPO/dist/credential-proxy.js"
            if [ ! -f "$_PROXY_SCRIPT" ]; then
                echo "ERROR: bundled mode but $_PROXY_SCRIPT missing — desktop packaging error" >&2
                echo "  The wrapper fail-closes on this too (exit 78); build:bundle must emit it." >&2
                exit 1
            fi
        else
            _PROXY_SCRIPT="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path skills/quota-tracker/scripts/credential-proxy.ts)"
            if [ ! -f "$_PROXY_SCRIPT" ]; then
                echo "ERROR: quota-tracker skill not found at $_PROXY_SCRIPT" >&2
                echo "  Install it first — credential-proxy.ts is the proxy target." >&2
                exit 1
            fi
        fi
        BREW_BIN="$(resolve_brew_bin)"
        # Baked in because launchd inherits no shell env; an empty value would
        # install a proxy that silently reads the vanilla keychain item.
        CLAUDE_CFG="$(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 bash "$REPO/scripts/sutando-config.sh" claude-home-path 2>/dev/null)"
        [ -n "$CLAUDE_CFG" ] || { echo "ERROR: could not resolve canonical Claude config directory" >&2; exit 1; }
        echo "Installing $LABEL"
        echo "  repo:      $REPO"
        echo "  workspace: $WORKSPACE"
        echo "  brew bin:  $BREW_BIN"
        echo "  config:    $CLAUDE_CFG"
        mkdir -p "$HOME/Library/LaunchAgents"
        mkdir -p "$WORKSPACE/logs"
        # Escaping renderer + resolved interpreter: a bare python3 can be the
        # Xcode-CLT stub, which passes an existence check and raises the dialog.
        . "$REPO/scripts/python-binary.sh"
        _PY="$(require_python "$REPO" "install the credential-proxy launchd job")" || exit 1
        "$_PY" "$REPO/src/render_plist_template.py" "$TEMPLATE" "$DEST" \
            "REPO=$REPO" \
            "WORKSPACE=$WORKSPACE" \
            "BREW_BIN=$BREW_BIN" \
            "SUTANDO_NODE=${SUTANDO_NODE:-}" \
            "CLAUDE_CONFIG_DIR=$CLAUDE_CFG" \
            "HOME=$HOME" || exit 1
        bootout_if_loaded
        launchctl bootstrap "$DOMAIN" "$DEST"
        echo "  Loaded via $SERVICE"
        echo
        echo "credential-proxy is now launchd-managed (KeepAlive, ThrottleInterval=10s)."
        echo "  • View status:  bash $0 --status"
        echo "  • Uninstall:    bash $0 --uninstall"
        echo "  • Logs:         $WORKSPACE/logs/credential-proxy.log"
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
