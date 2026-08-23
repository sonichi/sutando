#!/usr/bin/env bash
# Reconnecting a dropped gateway lane must not require running the whole of
# startup.sh — which reaps any live task watcher as a side effect (it assumes
# it runs once, at session start, before anything else starts a watcher).
# scripts/restart-gateway-lanes.sh calls only start_gateway_lanes() and must
# never touch the watcher sentinel or invoke the reaper.
#
# Run: bash tests/restart-gateway-lanes-no-watcher-touch.test.sh
# Exit: 0 = all pass, 1 = failure
set -uo pipefail

REPO="${REPO_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
fails=0

ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s — %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

echo "restart-gateway-lanes.sh watcher isolation:"

# --- wiring: the standalone script must exist and never reference the reaper --
if [ -x "$REPO/scripts/restart-gateway-lanes.sh" ]; then
  ok "scripts/restart-gateway-lanes.sh exists and is executable"
else
  bad "scripts/restart-gateway-lanes.sh exists and is executable" "missing or not +x"
fi

# Non-comment lines only — the header comment legitimately explains WHY this
# script exists relative to the reaper; what must never appear is a CALL.
if grep -v '^\s*#' "$REPO/scripts/restart-gateway-lanes.sh" | grep -q 'reap_stale_task_watcher'; then
  bad "restart-gateway-lanes.sh never calls the watcher reaper" \
    "a call was found — this script must stay watcher-blind"
else
  ok "restart-gateway-lanes.sh never calls the watcher reaper"
fi

if grep -q 'start_gateway_lanes' "$REPO/scripts/restart-gateway-lanes.sh"; then
  ok "restart-gateway-lanes.sh calls start_gateway_lanes"
else
  bad "restart-gateway-lanes.sh calls start_gateway_lanes" "call not found"
fi

# --- wiring: startup.sh delegates to the shared function, doesn't re-implement --
if grep -q '^start_gateway_lanes$' "$REPO/src/startup.sh"; then
  ok "startup.sh delegates to start_gateway_lanes"
else
  bad "startup.sh delegates to start_gateway_lanes" "call site not found"
fi
if grep -q 'AG2_REMOTE_TOKEN_\[A-Za-z0-9_\]' "$REPO/src/startup.sh"; then
  bad "startup.sh keeps no unguarded copy of the named-lane loop" "inline copy still present"
else
  ok "startup.sh keeps no unguarded copy of the named-lane loop"
fi

# --- wiring: start_gateway_lanes is defined and reap runs strictly BEFORE it --
# (order matters for a real boot: startup.sh must still clear a genuinely
# stale watcher from a prior crashed session before anything else runs.)
if declare -F start_gateway_lanes > /dev/null 2>&1; then
  : # already sourced by an earlier test in this run — fine
else
  # shellcheck source=../src/startup-runtime.sh
  source "$REPO/src/startup-runtime.sh"
fi
if declare -F start_gateway_lanes > /dev/null 2>&1; then
  ok "start_gateway_lanes is defined in src/startup-runtime.sh"
else
  bad "start_gateway_lanes is defined in src/startup-runtime.sh" "not found"
fi

reap_line="$(grep -n 'reap_stale_task_watcher "\$WORKSPACE' "$REPO/src/startup.sh" | head -1 | cut -d: -f1)"
gw_line="$(grep -n '^start_gateway_lanes$' "$REPO/src/startup.sh" | head -1 | cut -d: -f1)"
if [ -n "$reap_line" ] && [ -n "$gw_line" ] && [ "$reap_line" -lt "$gw_line" ]; then
  ok "startup.sh still reaps before (re)starting gateway lanes on a real boot"
else
  bad "startup.sh still reaps before (re)starting gateway lanes on a real boot" \
    "reap@$reap_line gateway@$gw_line"
fi

# --- behavior: calling start_gateway_lanes alone (unconfigured — a safe no-op)
# leaves an unrelated watcher sentinel completely untouched -------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PID_FILE="$TMP/watch-tasks-stream.pid"
echo "99999999" > "$PID_FILE"
before="$(cat "$PID_FILE")"

(
  # No AG2_REMOTE_TOKEN* in env and no channels/ag2space/.env visible ->
  # start_gateway_lanes's own guard makes it a silent no-op, same contract as
  # in startup.sh for an unconfigured host.
  unset AG2_REMOTE_TOKEN REMOTE_TASK_TOKEN REMOTE_TASK_TOKEN_FILE 2>/dev/null || true
  # Point claude-home-path resolution somewhere with no channels/ dir so this
  # stays hermetic regardless of the real host's own AG2 Space configuration.
  export CLAUDE_CONFIG_DIR="$TMP/no-such-config-dir"
  REPO="$REPO" PY="" LOGS_DIR="$TMP" start_gateway_lanes
) > "$TMP/out.log" 2>&1

if [ -f "$PID_FILE" ] && [ "$(cat "$PID_FILE")" = "$before" ]; then
  ok "unrelated watcher sentinel untouched by start_gateway_lanes"
else
  bad "unrelated watcher sentinel untouched by start_gateway_lanes" \
    "sentinel changed or vanished ($(cat "$TMP/out.log" 2>/dev/null))"
fi

if [ "$fails" -eq 0 ]; then
  echo "ALL PASS"
  exit 0
fi
echo "FAILED ($fails)"
exit 1
