#!/bin/bash
# Reconnect the AG2 Space gateway bridge (primary + every configured named
# secondary lane) WITHOUT running the rest of src/startup.sh.
#
# Why this exists: the named-instance lanes (AG2_REMOTE_TOKEN_<INST> in
# channels/ag2space/.env) are only durable through startup.sh — a plain
# supervisor restart brings back the primary lane and silently drops every
# named one. Before this script, the only way to reconnect a dropped lane was
# `bash src/startup.sh`, which ALSO runs reap_stale_task_watcher: on the
# assumption that startup.sh runs once, at session start, before anything
# else starts a watcher, it unconditionally kills any watcher already
# running. Re-running the whole boot sequence mid-session to fix a gateway
# lane took the live task watcher down as a side effect.
#
# Usage: bash scripts/restart-gateway-lanes.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# shellcheck source=python-binary.sh
. "$REPO/scripts/python-binary.sh"
PY="$(resolve_python "$REPO")"
if [ -z "$PY" ]; then
  echo "no runnable python3 found — cannot start gateway lanes" >&2
  exit 1
fi

# The credential store startup.sh uses. Left unset, the lanes read ~/.claude's channel
# env instead of the one the connect flow wrote, and a configured lane is not started.
source "$REPO/src/claude_config_dir.sh"
_ccd_rc=0
_ccd="$(resolve_claude_config_dir "$REPO" restart-gateway-lanes)" || _ccd_rc=$?
case "$_ccd_rc" in
  0|2) export CLAUDE_CONFIG_DIR="$_ccd" ;;
  *) exit 1 ;;
esac

WORKSPACE="$(bash "$REPO/scripts/sutando-config.sh" workspace)"
LOGS_DIR="$WORKSPACE/logs"
mkdir -p "$LOGS_DIR"

# shellcheck source=../src/startup-runtime.sh
source "$REPO/src/startup-runtime.sh"

start_gateway_lanes
if [ "$GATEWAY_LANES_CONFIGURED" != "1" ]; then
  echo "No gateway lane started: no gateway token in $CLAUDE_CONFIG_DIR/channels/ag2space or the environment. Task watcher untouched." >&2
  exit 1
fi
if [ "$GATEWAY_LANES_NOT_LAUNCHED" != "0" ]; then
  echo "$GATEWAY_LANES_NOT_LAUNCHED gateway lane(s) not launched, named above; see $LOGS_DIR/remote-gateway-bridge.log. Task watcher untouched." >&2
  exit 1
fi
echo "Gateway lane(s) (re)started where configured. Task watcher untouched."
