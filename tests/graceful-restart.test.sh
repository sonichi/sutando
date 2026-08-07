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
doubles=0; deferrals=0; reaped=0; bad_sentinel=0; rc_bad=0
for _i in $(seq 1 "$N_CONC"); do
  # DEAD core (no .alive): this is where the double-restart actually races.
  # With a live+idle core the pre-fix failure mode is STARVATION instead —
  # one run deletes the other's sentinel and the loser exits 3 — so a
  # live-core fixture reports "exactly one" against the BROKEN script and
  # the assertion proves nothing. Verified both ways before choosing this.
  WS0="$TMP/ws0-$_i"; mkdir -p "$WS0/state/cores" "$WS0/tasks"
  printf '{"status":"idle","ts":%s}\n' "$(date +%s)" > "$WS0/state/core-status.json"
  a_out="$TMP/conc-a-$_i.log"; b_out="$TMP/conc-b-$_i.log"
  ( GR_WS="$WS0" GR_SYNC_CMD="true" GR_POLL_S=0 GR_RETAIN_LOCK_ON_DECISION=1 bash "$GR" --dry-run >"$a_out" 2>&1 ) &
  pa=$!
  ( GR_WS="$WS0" GR_SYNC_CMD="true" GR_POLL_S=0 GR_RETAIN_LOCK_ON_DECISION=1 bash "$GR" --dry-run >"$b_out" 2>&1 ) &
  pb=$!
  ra=0; rb=0
  wait "$pa" || ra=$?
  wait "$pb" || rb=$?
  # Exit codes matter as much as stdout. A peer that DIES (set -e on a bad
  # arithmetic operand, say) also fails to restart, so the "exactly one
  # decision" count alone cannot distinguish a correct deferral from a crash.
  # Not hypothetical: CI caught exactly that when `stat -f %m` (BSD) emitted a
  # filesystem dump on GNU/Linux, the arithmetic died under `set -euo pipefail`,
  # and the peer vanished silently while "exactly one" still reported PASS.
  case "$ra:$rb" in
    0:4|4:0) : ;;
    *) rc_bad=$((${rc_bad:-0} + 1)) ;;
  esac
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
[ "$rc_bad" = 0 ] \
  && say ok "winner exited 0 and loser exited 4 in every pair" \
  || say FAIL "$rc_bad/$N_CONC pairs had an unexpected exit-code pair (a CRASHED peer is indistinguishable from a deferring one by count alone)"
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

# --- GNU-style `stat -f` must not break alive_age() (qingyun review, 2026-08-02) --------
# On GNU/Linux `stat -f` means --file-system: it prints a multi-line dump AND
# EXITS 0. Selecting the fallback on EXIT STATUS therefore never reaches
# `stat -c` on Linux, and a digit guard alone turns that into a CONFIDENT WRONG
# answer: every mtime reads unreadable, so alive_age() returns 999999 and EVERY
# core is classified DEAD.
#
# The first version of this case asserted only "does not crash / does not hang".
# A dead-reporting alive_age() satisfies all of that, so the case passed on macOS
# while CI went red. The assertion that matters is that a FRESH heartbeat is
# still read as FRESH under GNU-shaped stat — correctness, not absence of noise.
echo "8. GNU-style stat -f: a FRESH .alive must still read fresh (not DEAD)"
WS7="$TMP/ws7"; mkws "$WS7"                       # mkws touches a live .alive
printf '{"status":"running","step":"busy","ts":%s}\n' "$(date +%s)" > "$WS7/state/core-status.json"
FAKEBIN="$TMP/fakebin7"; mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/stat" <<'STATEOF'
#!/bin/bash
# Mimic GNU coreutils: -f is --file-system (multi-line dump, exit 0); -c is format.
if [ "${1:-}" = "-f" ]; then
  printf '  File: "%s"\n    ID: 0 Namelen: 255 Type: apfs\n' "${3:-${2:-}}"
  exit 0
fi
if [ "${1:-}" = "-c" ]; then
  # Return the REAL mtime. The HOST's /usr/bin/stat may itself be GNU (CI) or
  # BSD (macOS), so this fallback cannot hardcode either syntax — an earlier
  # revision hardcoded `-f %m` and the fixture silently yielded nothing on
  # ubuntu-latest, tripping this very test. Same output-shape rule as mtime_of.
  _f="${3:-}"
  for _real in "-c %Y" "-f %m"; do
    _o="$(/usr/bin/stat $_real "$_f" 2>/dev/null || true)"
    case "$_o" in ""|*[!0-9]*) ;; *) printf "%s\n" "$_o"; exit 0 ;; esac
  done
  exit 1
fi
exec /usr/bin/stat "$@"
STATEOF
chmod +x "$FAKEBIN/stat"
out="$(PATH="$FAKEBIN:$PATH" GR_WS="$WS7" GR_SYNC_CMD="true" GR_POLL_S=1 GR_STATUS_TTL_S=30 \
       GR_BUSY_MAX_S=3 bash "$GR" --dry-run 2>&1)"; rc=$?
# THE load-bearing assertion: the heartbeat is seconds old, so the run must not
# declare the core dead. Pre-fix (BSD-only) and mid-fix (digit-guard-only) both
# fail here; only output-shape selection passes.
echo "$out" | grep -q "core is DEAD" \
  && say FAIL "fresh .alive must NOT read as DEAD under GNU-shaped stat: $out" \
  || say ok "fresh .alive still reads fresh under GNU-shaped stat"
echo "$out" | grep -qiE 'integer expression|unbound variable|File:' \
  && say FAIL "no arithmetic/unbound diagnostic leaked: $out" \
  || say ok "no arithmetic/unbound diagnostic"
[ "$rc" = 0 ] && say ok "exit 0 under GNU-style stat" || say FAIL "exit 0 under GNU-style stat (got $rc): $out"

echo "9. A LIVE holder waiting longer than LOCK_STALE_S must NOT be reaped (qingyun review, #2334)"
# The quiet gate is deliberately unbounded on a healthy core, but lock staleness
# is judged from $LOCKDIR's mtime, which `mkdir` stamps ONCE. So a holder that
# waits past LOCK_STALE_S looked abandoned, and a peer reaped a LIVE lock and
# entered the restart decision concurrently — two destructive restarts.
# Here the wait (5s) deliberately exceeds LOCK_STALE_S (2s).
WS9="$TMP/ws9"; mkws "$WS9"
printf '{"status":"running","ts":%s}\n' "$(date +%s)" > "$WS9/state/core-status.json"
# Keep the core convincingly ALIVE and BUSY so the holder never exits the gate.
( for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    touch "$WS9/state/cores/$HOST.alive"
    printf '{"status":"running","ts":%s}\n' "$(date +%s)" > "$WS9/state/core-status.json"
    sleep 1
  done ) >/dev/null 2>&1 &
keeper=$!
( GR_WS="$WS9" GR_SYNC_CMD="true" GR_POLL_S=1 GR_LOCK_STALE_S=2 bash "$GR" --dry-run \
    >"$TMP/ws9_holder.out" 2>&1 ) &
holder=$!
sleep 5   # >> LOCK_STALE_S: an un-renewed lock now reads as abandoned
b_out="$(GR_WS="$WS9" GR_SYNC_CMD="true" GR_POLL_S=1 GR_LOCK_STALE_S=2 bash "$GR" --dry-run 2>&1)"
b_rc=$?
kill "$holder" "$keeper" 2>/dev/null || true
wait "$holder" "$keeper" 2>/dev/null || true
if [ "$b_rc" = 4 ] && printf '%s' "$b_out" | grep -q 'deferring'; then
  say ok "live holder waiting 5s > LOCK_STALE_S=2s was NOT reaped (peer deferred, rc=4)"
else
  say FAIL "peer REAPED a live holder's lock (rc=$b_rc) — concurrent destructive restart: $(printf '%s' "$b_out" | grep -i 'reap\|deferr' | tail -1)"
fi

echo "10. A holder that LOSES its lease must defer, not restart alongside the reaper (qingyun #2334)"
# His repro: A waits in the healthy gate; A stalls past LOCK_STALE_S (SIGSTOP models
# a scheduler stall); B legitimately reaps the now-stale lock and acquires it; A
# resumes. Before the ownership check, A touched B's lock and walked on to prep —
# both A and B exited 0 and both logged "would exec". Exactly one may decide.
WS10="$TMP/ws10"; mkws "$WS10"
printf '{"status":"running","ts":%s}\n' "$(date +%s)" > "$WS10/state/core-status.json"
( for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14; do
    touch "$WS10/state/cores/$HOST.alive"
    [ -f "$TMP/ws10_goidle" ] \
      && printf '{"status":"idle","ts":%s}\n' "$(date +%s)" > "$WS10/state/core-status.json" \
      || printf '{"status":"running","ts":%s}\n' "$(date +%s)" > "$WS10/state/core-status.json"
    sleep 1
  done ) >/dev/null 2>&1 &
keeper10=$!
( GR_WS="$WS10" GR_SYNC_CMD="true" GR_POLL_S=1 GR_LOCK_STALE_S=2 bash "$GR" --dry-run \
    >"$TMP/ws10_A.out" 2>&1 ) &
A=$!
sleep 2                      # let A acquire the lock and enter the gate
kill -STOP "$A" 2>/dev/null  # stall A past the stale threshold
sleep 4
( GR_WS="$WS10" GR_SYNC_CMD="true" GR_POLL_S=1 GR_LOCK_STALE_S=2 bash "$GR" --dry-run \
    >"$TMP/ws10_B.out" 2>&1 ) &
B=$!
sleep 3                      # B reaps the stale lock and takes it
kill -CONT "$A" 2>/dev/null  # A resumes holding a lease it no longer owns
touch "$TMP/ws10_goidle"     # release both from the busy gate
wait "$A" 2>/dev/null; wait "$B" 2>/dev/null
kill "$keeper10" 2>/dev/null || true; wait "$keeper10" 2>/dev/null || true
decisions=$(cat "$TMP/ws10_A.out" "$TMP/ws10_B.out" 2>/dev/null | grep -c "would exec" || true)
if [ "$decisions" = 1 ]; then
  say ok "exactly ONE restart decision after a lease loss (the resumed holder deferred)"
else
  say FAIL "$decisions restart decisions after a lease loss — concurrent destructive restart: $(grep -h 'would exec\|lost the restart lease' "$TMP/ws10_A.out" "$TMP/ws10_B.out" 2>/dev/null | tr '\n' ' ' | cut -c1-160)"
fi

echo "11. lost-lease holder → exit 4 (NOT 1), and the foreign lock is PRESERVED"
# cleanup_lock used to end in `own_lock && rm -rf "$LOCKDIR"`, whose status IS the
# function's status. On the lease-loss path the lock is no longer ours, so that is
# FALSE, and bash takes the EXIT trap's status as the script's — turning the
# documented `exit 4` defer into 1. A supervisor branching on 4 sees a crash.
WS11="$TMP/ws11"; mkws "$WS11"
printf '{"status":"running","step":"busy","ts":%s}\n' "$(date +%s)" > "$WS11/state/core-status.json"
( GR_WS="$WS11" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run > "$TMP/ws11.out" 2>&1; echo $? > "$TMP/ws11.rc" ) &
gr11=$!
LOCK11="$WS11/state/locks/graceful-restart.lock"
for _ in $(seq 1 60); do [ -f "$LOCK11/rid" ] && break; sleep 0.2; done
echo "some-other-holder" > "$LOCK11/rid"        # reaper took the lease out from under us
wait "$gr11" 2>/dev/null || true
rc11="$(cat "$TMP/ws11.rc" 2>/dev/null || echo missing)"
[ "$rc11" = 4 ] && say ok "lease-loss exits 4" \
  || say FAIL "lease-loss exits 4 (got $rc11) — the EXIT trap replaced the explicit status"
grep -q "lost the restart lease" "$TMP/ws11.out" \
  && say ok "lease-loss logged its reason" || say FAIL "lease-loss logged its reason: $(cat "$TMP/ws11.out" | tr '\n' ' ' | cut -c1-160)"
[ -d "$LOCK11" ] && say ok "the foreign lock is left alone" \
  || say FAIL "the foreign lock was deleted — a run that lost the lease must not free the new holder's lock"

echo "12. direct TERM → exit 143 (NOT 1), while still removing our OWN lock"
# Two distinct failures met here, and the second is why an owned-lock fixture is
# required: (a) `trap 'cleanup_lock; exit 143' TERM` under `set -e` aborts at a
# non-zero cleanup_lock, so `exit 143` is never reached; (b) even when cleanup
# SUCCEEDS, `exit 143` re-fires the EXIT trap, which runs cleanup_lock a SECOND
# time against a lock it just deleted — own_lock is now false, status 1, and it
# overrides the 143. Verified 1 -> 143 across this exact fixture.
WS12="$TMP/ws12"; mkws "$WS12"
printf '{"status":"running","step":"busy","ts":%s}\n' "$(date +%s)" > "$WS12/state/core-status.json"
( GR_WS="$WS12" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run > "$TMP/ws12.out" 2>&1; echo $? > "$TMP/ws12.rc" ) &
gr12=$!
LOCK12="$WS12/state/locks/graceful-restart.lock"
for _ in $(seq 1 60); do [ -f "$LOCK12/rid" ] && break; sleep 0.2; done
# TERM the script itself, not the subshell wrapper.
gr12_pid="$(pgrep -P "$gr12" -f "graceful-restart.sh" | head -1)"
[ -n "$gr12_pid" ] && kill -TERM "$gr12_pid" 2>/dev/null
wait "$gr12" 2>/dev/null || true
rc12="$(cat "$TMP/ws12.rc" 2>/dev/null || echo missing)"
[ "$rc12" = 143 ] && say ok "TERM exits 143" \
  || say FAIL "TERM exits 143 (got $rc12) — set -e aborted the trap, or the re-fired EXIT trap overrode it"
[ ! -d "$LOCK12" ] && say ok "our own lock is released on TERM" \
  || say FAIL "our own lock survived TERM — cleanup must not be skipped to make the exit code right"

echo "13. a normal --dry-run must NOT self-block the next real run"
# Verifying a destructive command before firing it is the natural operator
# sequence, so a dry-run that keeps the lock blocks the real run for
# LOCK_STALE_S (900s). Retention is still required for the concurrency model,
# which now asks for it via GR_RETAIN_LOCK_ON_DECISION.
WS13="$TMP/ws13"; mkws "$WS13"
out13a="$(GR_WS="$WS13" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run 2>&1 || true)"
echo "$out13a" | grep -q "DRY-RUN — would exec" || say FAIL "dry-run did not reach a restart decision: $out13a"
LOCK13="$WS13/state/locks/graceful-restart.lock"
[ ! -d "$LOCK13" ] && say ok "a decided dry-run released its own lock" \
  || say FAIL "dry-run retained the lock — the next real run self-blocks for LOCK_STALE_S"

# The load-bearing assertion: the FOLLOWING run must not defer.
out13b="$(GR_WS="$WS13" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run 2>&1)"; rc13b=$?
[ "$rc13b" = 0 ] && say ok "the run immediately after a dry-run proceeds (rc 0)" \
  || say FAIL "the run after a dry-run exited $rc13b (4 = self-deferred on its own stale lock)"
echo "$out13b" | grep -q "another restart is in progress" \
  && say FAIL "the following run deferred to its own predecessor's lock" \
  || say ok "no self-deferral in the following run"

# ...and the retain mode still works, or the concurrency case above is hollow.
WS13c="$TMP/ws13c"; mkws "$WS13c"
GR_WS="$WS13c" GR_SYNC_CMD="true" GR_POLL_S=1 GR_RETAIN_LOCK_ON_DECISION=1 bash "$GR" --dry-run >/dev/null 2>&1 || true
[ -d "$WS13c/state/locks/graceful-restart.lock" ] \
  && say ok "GR_RETAIN_LOCK_ON_DECISION=1 still retains (models production exec)" \
  || say FAIL "retain mode did not retain — the concurrency test no longer models production"

if [ "$fails" = 0 ]; then
  echo "ALL PASS"
else
  echo "$fails FAILURE(S)"
  exit 1
fi
