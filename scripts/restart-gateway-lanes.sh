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

WORKSPACE="$(bash "$REPO/scripts/sutando-config.sh" workspace)"
LOGS_DIR="$WORKSPACE/logs"
mkdir -p "$LOGS_DIR"

# shellcheck source=../src/startup-runtime.sh
source "$REPO/src/startup-runtime.sh"

start_gateway_lanes
echo "Gateway lane(s) (re)started where configured. Task watcher untouched."
