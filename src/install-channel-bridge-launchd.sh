#!/bin/bash
# Install / uninstall / inspect a launchd-supervised channel bridge.
#
# Usage:
#   bash src/install-channel-bridge-launchd.sh slack
#   bash src/install-channel-bridge-launchd.sh discord --uninstall
#   bash src/install-channel-bridge-launchd.sh telegram --status

set -e

CHANNEL="${1:-}"
ACTION="${2:-install}"
case "$CHANNEL" in
  slack|discord|telegram) ;;
  *) echo "Usage: $0 {slack|discord|telegram} [install|--uninstall|--status]" >&2; exit 2 ;;
esac

LABEL="com.sutando.$CHANNEL-bridge"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/src/launchd/com.sutando.channel-bridge.plist"
WRAPPER="$REPO/src/launchd/channel-bridge-wrapper.sh"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"

# shellcheck source=workspace_resolve.sh
source "$REPO/src/workspace_resolve.sh"
resolve_workspace_or_die

bootout_if_loaded() {
  if launchctl print "$SERVICE" >/dev/null 2>&1; then
    launchctl bootout "$SERVICE" 2>/dev/null || true
    for _ in $(seq 1 10); do
      launchctl print "$SERVICE" >/dev/null 2>&1 || break
      sleep 0.3
    done
  fi
}

case "$ACTION" in
  install)
    [ -f "$TEMPLATE" ] || { echo "ERROR: template not found: $TEMPLATE" >&2; exit 1; }
    [ -f "$WRAPPER" ] || { echo "ERROR: wrapper not found: $WRAPPER" >&2; exit 1; }
    CLAUDE_CFG="$(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 bash "$REPO/scripts/sutando-config.sh" claude-home-path 2>/dev/null)"
    [ -n "$CLAUDE_CFG" ] || CLAUDE_CFG="$CLAUDE_CONFIG_DIR"
    [ -n "$CLAUDE_CFG" ] || CLAUDE_CFG="$HOME/.claude"
    # Resolve the interpreter's bin dir from the installer's own PATH — host-
    # agnostic, no arch/user-specific literal (see install-gateway's resolve_brew_bin).
    # Substituted into the plist PATH (__BREW_BIN__) so the launchd wrapper finds
    # python3 without re-probing hardcoded locations at runtime.
    _py="$(command -v python3 2>/dev/null)" || _py=""
    if [ -n "$_py" ]; then BREW_BIN="$(dirname "$_py")"; else BREW_BIN=/usr/bin; fi
    mkdir -p "$HOME/Library/LaunchAgents" "$WORKSPACE/logs" "$WORKSPACE/state/channel-bridge-supervisor"
    # Suppress the intentional installer reload from looking like a crash.
    rm -f "$WORKSPACE/state/channel-bridge-supervisor/$CHANNEL.started"
    export CHANNEL LABEL REPO WORKSPACE BREW_BIN CLAUDE_CFG
    python3 - "$TEMPLATE" "$DEST" <<'PY'
import os, plistlib, sys
src, dst = sys.argv[1:]
with open(src, "rb") as fh:
    data = plistlib.load(fh)
replacements = {
    "__CHANNEL__": os.environ["CHANNEL"],
    "__LABEL__": os.environ["LABEL"],
    "__REPO__": os.environ["REPO"],
    "__WORKSPACE__": os.environ["WORKSPACE"],
    "__HOME__": os.environ["HOME"],
    "__BREW_BIN__": os.environ["BREW_BIN"],
    "__CLAUDE_CONFIG_DIR__": os.environ["CLAUDE_CFG"],
}
def replace(value):
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
    elif isinstance(value, list):
        value = [replace(v) for v in value]
    elif isinstance(value, dict):
        value = {k: replace(v) for k, v in value.items()}
    return value
with open(dst, "wb") as fh:
    plistlib.dump(replace(data), fh, sort_keys=False)
PY
    bootout_if_loaded
    launchctl bootstrap "$DOMAIN" "$DEST"
    # RunAtLoad may remain "pended nondemand spawn = speculative" after a
    # programmatic bootstrap on current macOS. Kickstart makes installation
    # synchronous enough for startup.sh's PID verification instead of silently
    # leaving a loaded-but-never-started job.
    launchctl kickstart "$SERVICE"
    echo "$CHANNEL bridge is launchd-supervised (KeepAlive, 10s throttle)."
    ;;
  --uninstall|uninstall)
    bootout_if_loaded
    [ ! -f "$DEST" ] || rm "$DEST"
    ;;
  --status|status)
    if launchctl print "$SERVICE" >/dev/null 2>&1; then
      launchctl print "$SERVICE" | grep -E '^\s+(state|pid|last exit code|runs|path)' || true
    else
      echo "(not loaded)"
    fi
    ;;
  *) echo "Usage: $0 {slack|discord|telegram} [install|--uninstall|--status]" >&2; exit 2 ;;
esac
