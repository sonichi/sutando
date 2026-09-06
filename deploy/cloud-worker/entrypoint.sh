#!/usr/bin/env bash
# One cloud seat: the gateway client in cloud mode + the seat runtime named by
# SUTANDO_WORKER_RUNTIME. Exit: 2 env/usage, 3 workspace, 4 runtime unavailable, 5 child exited.
set -uo pipefail

ENGINE="${SUTANDO_ENGINE_DIR:-/app/engine}"
WS="${SUTANDO_CLOUD_WORKSPACE:-/workspace}"
HERE="$ENGINE/deploy/cloud-worker"

missing=()
for v in REMOTE_TASK_URL REMOTE_TASK_TOKEN SUTANDO_WORKER_ID; do
  [ -n "${!v:-}" ] || missing+=("$v")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "cloud-worker: missing required env: ${missing[*]}" >&2
  exit 2
fi

export GATEWAY_INSTANCE="${GATEWAY_INSTANCE:-cloud}"
export SUTANDO_WORKER_LOCATION=cloud
export SUTANDO_SUPERVISED=1
# No default: a seat that answers must be chosen deliberately. Defaulting to the
# test double let the documented quickstart post "answered by ..." as a real result.
RUNTIME="${SUTANDO_WORKER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
  echo "cloud-worker: SUTANDO_WORKER_RUNTIME is required (claude | ag2-assistant | adapter | stub)" >&2
  exit 2
fi
if [ "$RUNTIME" = stub ] && [ "${SUTANDO_ALLOW_STUB_SEAT:-}" != "1" ]; then
  echo "cloud-worker: runtime 'stub' is a TEST DOUBLE and would post 'answered by ...' as a real result; set SUTANDO_ALLOW_STUB_SEAT=1 to permit it" >&2
  exit 2
fi

# The workspace lives on the volume; the resolver reads this file beside the engine.
if ! mkdir -p "$WS/tasks" "$WS/results" "$WS/state" "$WS/logs"; then
  echo "cloud-worker: cannot create workspace dirs under $WS" >&2
  exit 3
fi
if ! printf '{"workspace": {"path": "%s"}}\n' "$WS" > "$ENGINE/sutando.config.local.json"; then
  echo "cloud-worker: cannot write $ENGINE/sutando.config.local.json" >&2
  exit 3
fi
resolved="$(bash "$ENGINE/scripts/sutando-config.sh" workspace 2>/dev/null)" || resolved=""
if [ "$resolved" != "$WS" ]; then
  echo "cloud-worker: workspace resolves to '${resolved:-<none>}', expected '$WS'" >&2
  exit 3
fi

cd "$ENGINE" || exit 3
echo "cloud-worker: seat=$SUTANDO_WORKER_ID instance=$GATEWAY_INSTANCE runtime=$RUNTIME workspace=$WS"

python3 "$ENGINE/src/remote-gateway-bridge.py" &
client=$!

seat=""
case "$RUNTIME" in
  stub)
    python3 "$HERE/seat-stub.py" &
    seat=$!
    ;;
  claude)
    if ! command -v claude >/dev/null 2>&1; then
      echo "cloud-worker: runtime 'claude' needs the claude CLI on PATH — build with --target claude" >&2
      kill "$client" 2>/dev/null; exit 4
    fi
    export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$WS/claude-config}"
    mkdir -p "$CLAUDE_CONFIG_DIR"
    # A full engine checkout on the volume gives the skill its scripts/; else the minimal tree.
    seat_cwd="$ENGINE"
    [ -d "$WS/engine" ] && seat_cwd="$WS/engine"
    (cd "$seat_cwd" && exec claude --dangerously-skip-permissions --add-dir "$WS" -- "/proactive-loop") &
    seat=$!
    ;;
  ag2-assistant)
    if [ -z "${AG2ASSISTANT_ACP_TOKEN:-}" ]; then
      echo "cloud-worker: runtime 'ag2-assistant' needs AG2ASSISTANT_ACP_TOKEN (the sidecar's --token)" >&2
      kill "$client" 2>/dev/null; exit 2
    fi
    export AG2ASSISTANT_ACP_URL="${AG2ASSISTANT_ACP_URL:-ws://assistant:8802}"
    python3 "$HERE/seat-ag2-assistant.py" &
    seat=$!
    ;;
  adapter)
    if [ ! -x "$WS/runtime.sh" ]; then
      echo "cloud-worker: runtime 'adapter' needs an executable $WS/runtime.sh (Agent SDK / codex app-server adapters are not shipped yet)" >&2
      kill "$client" 2>/dev/null; exit 4
    fi
    bash "$WS/runtime.sh" &
    seat=$!
    ;;
  *)
    echo "cloud-worker: unknown SUTANDO_WORKER_RUNTIME='$RUNTIME' (stub | claude | ag2-assistant | adapter)" >&2
    kill "$client" 2>/dev/null; exit 2
    ;;
esac

stopping=0
stop_all() { stopping=1; kill "$client" "$seat" 2>/dev/null; }
trap stop_all TERM INT

# No `wait -n` on bash 3: poll both children; the first to die ends the seat.
while kill -0 "$client" 2>/dev/null && kill -0 "$seat" 2>/dev/null; do sleep 1; done
if [ "$stopping" -eq 1 ]; then
  wait; echo "cloud-worker: stopped"; exit 0
fi
if kill -0 "$client" 2>/dev/null; then who="seat runtime ($RUNTIME)"; wait "$seat"; rc=$?
else who="gateway client"; wait "$client"; rc=$?; fi
echo "cloud-worker: a child exited — $who rc=$rc — stopping the seat so the restart policy relaunches it" >&2
kill "$client" "$seat" 2>/dev/null
wait
exit 5
