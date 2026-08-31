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
#   bash src/install-credential-proxy-launchd.sh is-current  # exit 0 if loaded job matches
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

# Load the interpreter resolver once. Lazy, not top-level: `set -e` is on, so a
# top-level source aborts before an earlier refusal can name its own reason.
_load_python_helper() {
    [ -n "${_PY_HELPER_LOADED:-}" ] && return 0
    # shellcheck source=../scripts/python-binary.sh
    . "$REPO/scripts/python-binary.sh" || return 1
    _PY_HELPER_LOADED=1
}

# Render the template to $1 with this checkout's values. ONE renderer, shared with
# is-current, so the check can never compare fewer fields than the install writes.
render_plist() {
    local _brew _ccd _py
    _brew="$(resolve_brew_bin)"
    # The RESOLVED dir, not the raw var: an unset var must pin the default the install
    # validated, not an empty string.
    _ccd="$(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 bash "$REPO/scripts/sutando-config.sh" claude-home-path 2>/dev/null)"
    [ -n "$_ccd" ] || { echo "ERROR: could not resolve canonical Claude config directory" >&2; return 1; }
    _load_python_helper || return 1
    _py="$(require_python "$REPO" "render the credential-proxy launchd plist")" || return 1
    "$_py" "$REPO/src/render_plist_template.py" "$TEMPLATE" "$1" \
        "REPO=$REPO" \
        "WORKSPACE=$WORKSPACE" \
        "BREW_BIN=$_brew" \
        "SUTANDO_NODE=${SUTANDO_NODE:-}" \
        "CLAUDE_CONFIG_DIR=$_ccd" \
        "HOME=$HOME"
}

# Semantic plist equality. plistlib, not PlistBuddy: the latter is macOS-only, so on
# Linux CI every read returns empty and any comparison passes vacuously.
# resolve_python, not require_python: is-current runs on every boot and must stay
# silent — no interpreter means "cannot compare", which the caller treats as drift.
plists_equal() {
    local _py
    _load_python_helper || return 1
    _py="$(resolve_python "$REPO")"
    [ -n "$_py" ] || return 1
    "$_py" - "$1" "$2" <<'PLISTPY'
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as a, open(sys.argv[2], "rb") as b:
        sys.exit(0 if plistlib.load(a) == plistlib.load(b) else 1)
except Exception:
    sys.exit(1)
PLISTPY
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
            # Run the proxy from THIS checkout: it resolves its workspace file-relatively,
            # so another clone's copy would write quota-state.json where nothing reads it.
            _PROXY_SCRIPT="$REPO/skills/quota-tracker/scripts/credential-proxy.ts"
            if [ ! -f "$_PROXY_SCRIPT" ]; then
                _PROXY_SCRIPT="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path skills/quota-tracker/scripts/credential-proxy.ts)"
            fi
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
        render_plist "$DEST" || exit 1
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
    is-current|--is-current)
        # Exit 0 only if the loaded plist equals what an install would render NOW.
        # Whole-plist, not named fields: enumerating them is what missed the last two.
        launchctl print "$SERVICE" >/dev/null 2>&1 || exit 1
        [ -f "$DEST" ] || exit 1
        [ -f "$TEMPLATE" ] || exit 1
        _want="$(mktemp)"
        trap 'rm -f "$_want"' EXIT
        render_plist "$_want" || exit 1
        plists_equal "$DEST" "$_want" || exit 1
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
        echo "Usage: $0 [install|--uninstall|--status|is-current]" >&2
        exit 2
        ;;
esac
