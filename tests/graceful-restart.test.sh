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

echo "0. CONCURRENT double-click → EXACTLY ONE restart decision (sonichi#2334 review)"
# The sentinels are per-WORKSPACE and every run clears them at startup, so two
# orchestrators could each validate a sentinel carrying their OWN rid and both
# reach the destructive restart — the second killing the core the first just
# relaunched. Measured on the pre-fix script: 50/50 iterations restarted twice.
#
# NOT a timing-luck test: the two processes are started before either can
# finish, and the assertion is on the INVARIANT (exactly one decision), so a
# scheduler that serializes them still fails the run if both decide.
# ITERATE. A single pair is timing-dependent: verified that with a live+idle
# core one unfixed pair often lets only one through by luck, so a 1-shot check
# PASSES against the broken script and proves nothing. N pairs, and the run
# fails if ANY pair produced two decisions.
N_CONC="${GR_TEST_CONC_ITERS:-10}"
doubles=0; deferrals=0; reaped=0; bad_sentinel=0
for _i in $(seq 1 "$N_CONC"); do
  # DEAD core (no .alive): this is where the double-restart actually races.
  # With a live+idle core the pre-fix failure mode is STARVATION instead —
  # one run deletes the other's sentinel and the loser exits 3 — so a
  # live-core fixture reports "exactly one" against the BROKEN script and
  # the assertion proves nothing. Verified both ways before choosing this.
  WS0="$TMP/ws0-$_i"; mkdir -p "$WS0/state/cores" "$WS0/tasks"
  printf '{"status":"idle","ts":%s}\n' "$(date +%s)" > "$WS0/state/core-status.json"
  a_out="$TMP/conc-a-$_i.log"; b_out="$TMP/conc-b-$_i.log"
  ( GR_WS="$WS0" GR_SYNC_CMD="true" GR_POLL_S=0 bash "$GR" --dry-run >"$a_out" 2>&1 ) &
  pa=$!
  ( GR_WS="$WS0" GR_SYNC_CMD="true" GR_POLL_S=0 bash "$GR" --dry-run >"$b_out" 2>&1 ) &
  pb=$!
  wait "$pa" || true
  wait "$pb" || true
  d=0
  grep -q "would exec" "$a_out" && d=$((d + 1))
  grep -q "would exec" "$b_out" && d=$((d + 1))
  [ "$d" -ge 2 ] && doubles=$((doubles + 1))
  cat "$a_out" "$b_out" | grep -q "another restart is in progress" && deferrals=$((deferrals + 1))
  cat "$a_out" "$b_out" | grep -q "reaping stale restart lock" && reaped=$((reaped + 1))
  if [ ! -f "$WS0/state/restart-ready.json" ] || \
     ! grep -q '"restart_id":"grp-' "$WS0/state/restart-ready.json" 2>/dev/null; then
    bad_sentinel=$((bad_sentinel + 1))
  fi
done
[ "$doubles" = 0 ] \
  && say ok "exactly one restart decision in all $N_CONC concurrent pairs" \
  || say FAIL "$doubles/$N_CONC pairs BOTH restarted — the #2334 race"
[ "$deferrals" = "$N_CONC" ] \
  && say ok "the losing peer deferred with a reason every time" \
  || say FAIL "peer deferred with a reason in only $deferrals/$N_CONC pairs"
# A LIVE lock must never be reaped: an unreadable age has to fail CLOSED. The
# first cut of this fix wrote the holder ts AFTER mkdir, so a peer landing in
# that gap read no ts, computed age=now-0, called a 1s-old lock stale and reaped
# it — both then restarted. Age now comes from the lock dir's own mtime, which
# mkdir sets atomically as part of claiming the lock.
[ "$reaped" = 0 ] \
  && say ok "no live lock was reaped in $N_CONC pairs" \
  || say FAIL "$reaped/$N_CONC pairs reaped a LIVE lock (ts-written-after-mkdir race)"
# One run must not be able to delete or overwrite the other's terminal state.
[ "$bad_sentinel" = 0 ] \
  && say ok "surviving sentinel intact + rid-stamped in all $N_CONC pairs" \
  || say FAIL "$bad_sentinel/$N_CONC pairs lost or corrupted the terminal sentinel"

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
