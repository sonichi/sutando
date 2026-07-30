#!/bin/bash
# Regression tests for the deterministic graceful-restart flow (sonichi#2334).
#
# Covers the review blockers:
#   1. Deterministic handoff — the orchestrator itself produces a terminal
#      sentinel (ready/failed) with NO task-queue/LLM step in the loop.
#   2. Space-path safety — restart-prep.sh works from a checkout path
#      containing spaces (production argv path), and the GR_SYNC_CMD test
#      seam carries space-containing paths.
# Plus the gate branches: quiet, busy-then-idle, wedged (stale status), dead.
#
# All runs are --dry-run: the machinery executes end-to-end, the kill is skipped.
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
GR="$REPO/src/agent/graceful-restart.sh"
HOST="$(bash "$REPO/scripts/sutando-config.sh" host-label)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fails=0
say() {
  if [ "$1" = ok ]; then
    echo "  ok: $2"
  else
    echo "  FAIL: $2"
    fails=$((fails + 1))
  fi
  return 0
}

mkws() {  # fresh workspace with a live heartbeat + idle status
  local ws="$1"
  mkdir -p "$ws/state/cores" "$ws/tasks"
  touch "$ws/state/cores/$HOST.alive"
  printf '{"status":"idle","ts":%s}\n' "$(date +%s)" > "$ws/state/core-status.json"
}

echo "1. quiet core → prep runs directly → ready sentinel → dry-run restart"
WS1="$TMP/ws1"; mkws "$WS1"
out="$(GR_WS="$WS1" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run 2>&1)"; rc=$?
[ "$rc" = 0 ] && say ok "exit 0" || say FAIL "exit 0 (got $rc): $out"
echo "$out" | grep -q "DRY-RUN — would exec" && say ok "reached restart" || say FAIL "reached restart: $out"
echo "$out" | grep -q "prep-ready" && say ok "prep-ready reason" || say FAIL "prep-ready reason: $out"
[ -f "$WS1/state/restart-ready.json" ] && say ok "ready sentinel written" || say FAIL "ready sentinel written"
grep -q '"restart_id":"grp-' "$WS1/state/restart-ready.json" 2>/dev/null \
  && say ok "sentinel carries restart_id" || say FAIL "sentinel carries restart_id"
echo "$out" | grep -q "no task-queue handoff" && say ok "direct invocation logged" || say FAIL "direct invocation logged"

echo "2. busy core (fresh running status) → orchestrator WAITS, proceeds after idle flip"
WS2="$TMP/ws2"; mkws "$WS2"
printf '{"status":"running","step":"x","ts":%s}\n' "$(date +%s)" > "$WS2/state/core-status.json"
( sleep 3; printf '{"status":"idle","ts":%s}\n' "$(date +%s)" > "$WS2/state/core-status.json"
  touch "$WS2/state/cores/$HOST.alive" ) &
flip=$!
start=$(date +%s)
out="$(GR_WS="$WS2" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run 2>&1)"; rc=$?
took=$(( $(date +%s) - start ))
wait "$flip" 2>/dev/null
[ "$rc" = 0 ] && say ok "exit 0" || say FAIL "exit 0 (got $rc): $out"
[ "$took" -ge 2 ] && say ok "waited for the idle flip (${took}s)" || say FAIL "waited for the idle flip (took ${took}s — gate did not hold)"
echo "$out" | grep -q "prep-ready" && say ok "graceful after wait" || say FAIL "graceful after wait: $out"

echo "3. wedged core (running status with STALE ts) → does NOT wait forever"
WS3="$TMP/ws3"; mkws "$WS3"
printf '{"status":"running","step":"wedged","ts":%s}\n' "$(( $(date +%s) - 100 ))" > "$WS3/state/core-status.json"
start=$(date +%s)
out="$(GR_WS="$WS3" GR_SYNC_CMD="true" GR_POLL_S=1 GR_STATUS_TTL_S=5 bash "$GR" --dry-run 2>&1)"; rc=$?
took=$(( $(date +%s) - start ))
[ "$rc" = 0 ] && say ok "exit 0" || say FAIL "exit 0 (got $rc): $out"
[ "$took" -le 10 ] && say ok "proceeded promptly (${took}s)" || say FAIL "proceeded promptly (took ${took}s)"
echo "$out" | grep -q "DRY-RUN — would exec" && say ok "restarted despite wedged status" || say FAIL "restarted despite wedged status: $out"

echo "4. prep FAILURE on a live core → exit 3, NO restart, failed sentinel"
WS4="$TMP/ws4"; mkws "$WS4"
out="$(GR_WS="$WS4" GR_SYNC_CMD="false" GR_POLL_S=1 bash "$GR" --dry-run 2>&1)"; rc=$?
[ "$rc" = 3 ] && say ok "exit 3" || say FAIL "exit 3 (got $rc): $out"
echo "$out" | grep -q "DRY-RUN — would exec" && say FAIL "must NOT restart" || say ok "did not restart"
[ -f "$WS4/state/restart-prep-failed.json" ] && say ok "failed sentinel written" || say FAIL "failed sentinel written"

echo "5. DEAD core (stale .alive) → no wait; restart even if prep fails"
WS5="$TMP/ws5"; mkws "$WS5"
rm -f "$WS5/state/cores/$HOST.alive"
printf '{"status":"running","step":"ghost","ts":%s}\n' "$(date +%s)" > "$WS5/state/core-status.json"
out="$(GR_WS="$WS5" GR_SYNC_CMD="false" GR_POLL_S=1 bash "$GR" --dry-run 2>&1)"; rc=$?
[ "$rc" = 0 ] && say ok "exit 0" || say FAIL "exit 0 (got $rc): $out"
echo "$out" | grep -q "agent-dead-abrupt" && say ok "dead-abrupt path" || say FAIL "dead-abrupt path: $out"
echo "$out" | grep -q "DRY-RUN — would exec" && say ok "restarted" || say FAIL "restarted: $out"

echo "6. SPACE-PATH regression: restart-prep.sh from a checkout path with spaces (production argv path)"
SP="$TMP/repo with space"
mkdir -p "$SP/src/agent" "$SP/scripts"
cp "$REPO/src/agent/restart-prep.sh" "$SP/src/agent/"
printf '#!/bin/bash\ntouch "%s/sync-ran.marker"\n' "$TMP" > "$SP/scripts/sync-workspace.sh"
WS6="$TMP/ws6"; mkdir -p "$WS6/state" "$WS6/tasks"
out="$(GR_WS="$WS6" bash "$SP/src/agent/restart-prep.sh" test-space-rid 2>&1)"; rc=$?
[ "$rc" = 0 ] && say ok "prep exit 0 from spaced path" || say FAIL "prep exit 0 from spaced path (got $rc): $out"
[ -f "$TMP/sync-ran.marker" ] && say ok "spaced-path sync script actually ran" || say FAIL "spaced-path sync script actually ran"
grep -q '"restart_id":"test-space-rid"' "$WS6/state/restart-ready.json" 2>/dev/null \
  && say ok "ready sentinel from spaced path" || say FAIL "ready sentinel from spaced path"

echo "7. GR_SYNC_CMD seam is argv-safe for space-containing paths"
SPD="$TMP/dir with space"
mkdir -p "$SPD"
printf '#!/bin/bash\nexit 0\n' > "$SPD/ok.sh"
WS7="$TMP/ws7"; mkdir -p "$WS7/state" "$WS7/tasks"
out="$(GR_WS="$WS7" GR_SYNC_CMD="bash '$SPD/ok.sh'" bash "$REPO/src/agent/restart-prep.sh" seam-rid 2>&1)"; rc=$?
[ "$rc" = 0 ] && say ok "seam exit 0 with spaced path" || say FAIL "seam exit 0 with spaced path (got $rc): $out"
grep -q '"restart_id":"seam-rid"' "$WS7/state/restart-ready.json" 2>/dev/null \
  && say ok "seam ready sentinel" || say FAIL "seam ready sentinel"

echo
if [ "$fails" = 0 ]; then
  echo "ALL PASS"
else
  echo "$fails FAILURE(S)"
  exit 1
fi
