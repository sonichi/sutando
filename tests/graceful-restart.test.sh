#!/bin/bash
# Regression tests for the deterministic graceful-restart flow. Unless a case
# says otherwise, runs are --dry-run: the flow executes, the kill is skipped.
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

echo "0. CONCURRENT double-click → EXACTLY ONE restart decision"
# Asserts the INVARIANT (exactly one decision), not timing. Iterated: one pair
# can pass against the broken script by luck, so a 1-shot check proves nothing.
N_CONC="${GR_TEST_CONC_ITERS:-10}"
doubles=0; deferrals=0; reaped=0; bad_sentinel=0; rc_bad=0
for _i in $(seq 1 "$N_CONC"); do
  # DEAD core (no .alive) is where the double-restart races. A live+idle fixture
  # fails by STARVATION instead and reports "exactly one" against a broken script.
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
  # Check exit codes too: a peer that CRASHES also fails to restart, so the
  # decision count alone cannot tell a correct deferral from a dead process.
  case "$ra:$rb" in
    5:4|4:5) : ;;
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
  || say FAIL "$doubles/$N_CONC pairs BOTH restarted — the serialization race"
[ "$deferrals" = "$N_CONC" ] \
  && say ok "the losing peer deferred with a reason every time" \
  || say FAIL "peer deferred with a reason in only $deferrals/$N_CONC pairs"
[ "$rc_bad" = 0 ] \
  && say ok "winner exited 5 (dry run) and loser exited 4 in every pair" \
  || say FAIL "$rc_bad/$N_CONC pairs had an unexpected exit-code pair (a CRASHED peer is indistinguishable from a deferring one by count alone)"
# A LIVE lock must never be reaped, so an unreadable age fails CLOSED. Age comes
# from the lock dir's own mtime, which mkdir sets atomically with the claim.
[ "$reaped" = 0 ] \
  && say ok "no live lock was reaped in $N_CONC pairs" \
  || say FAIL "$reaped/$N_CONC pairs reaped a LIVE lock (ts-written-after-mkdir race)"
# One run must not be able to delete or overwrite the other's terminal state.
[ "$bad_sentinel" = 0 ] \
  && say ok "surviving sentinel intact + rid-stamped in all $N_CONC pairs" \
  || say FAIL "$bad_sentinel/$N_CONC pairs lost or corrupted the terminal sentinel"

# A dry run reaches the restart decision and exits 5, not 0: the menu-bar app
# maps the status to a notification and every 0 read as "Core restarted".
DRY_OK=5

echo "1. quiet core → prep runs directly → ready sentinel → dry-run restart"
WS1="$TMP/ws1"; mkws "$WS1"
out="$(GR_WS="$WS1" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run 2>&1)"; rc=$?
[ "$rc" = "$DRY_OK" ] && say ok "exit $DRY_OK (dry run)" || say FAIL "exit $DRY_OK (got $rc): $out"
echo "$out" | grep -q "DRY-RUN — would exec" && say ok "reached restart" || say FAIL "reached restart: $out"
echo "$out" | grep -q "prep-ready" && say ok "prep-ready reason" || say FAIL "prep-ready reason: $out"
[ -f "$WS1/state/restart-ready.json" ] && say ok "ready sentinel written" || say FAIL "ready sentinel written"
grep -q '"restart_id":"grp-' "$WS1/state/restart-ready.json" 2>/dev/null \
  && say ok "sentinel carries restart_id" || say FAIL "sentinel carries restart_id"
echo "$out" | grep -q "orchestrator-side" && say ok "orchestrator-side prep logged" || say FAIL "orchestrator-side prep logged"

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
[ "$rc" = "$DRY_OK" ] && say ok "exit $DRY_OK (dry run)" || say FAIL "exit $DRY_OK (got $rc): $out"
[ "$took" -ge 2 ] && say ok "waited for the idle flip (${took}s)" || say FAIL "waited for the idle flip (took ${took}s — gate did not hold)"
echo "$out" | grep -q "prep-ready" && say ok "graceful after wait" || say FAIL "graceful after wait: $out"

echo "3. wedged core (running status with STALE ts) → does NOT wait forever"
WS3="$TMP/ws3"; mkws "$WS3"
printf '{"status":"running","step":"wedged","ts":%s}\n' "$(( $(date +%s) - 100 ))" > "$WS3/state/core-status.json"
start=$(date +%s)
out="$(GR_WS="$WS3" GR_SYNC_CMD="true" GR_POLL_S=1 GR_STATUS_TTL_S=5 bash "$GR" --dry-run 2>&1)"; rc=$?
took=$(( $(date +%s) - start ))
[ "$rc" = "$DRY_OK" ] && say ok "exit $DRY_OK (dry run)" || say FAIL "exit $DRY_OK (got $rc): $out"
[ "$took" -le 10 ] && say ok "proceeded promptly (${took}s)" || say FAIL "proceeded promptly (took ${took}s)"
echo "$out" | grep -q "DRY-RUN — would exec" && say ok "restarted despite wedged status" || say FAIL "restarted despite wedged status: $out"

echo "4. prep FAILURE on a live core → exit 3, NO restart, failed sentinel"
WS4="$TMP/ws4"; mkws "$WS4"
out="$(GR_WS="$WS4" GR_SYNC_CMD="false" GR_POLL_S=1 bash "$GR" --dry-run 2>&1)"; rc=$?
[ "$rc" = 3 ] && say ok "exit 3" || say FAIL "exit 3 (got $rc): $out"
echo "$out" | grep -q "DRY-RUN — would exec" && say FAIL "must NOT restart" || say ok "did not restart"
[ -f "$WS4/state/restart-prep-failed.json" ] && say ok "failed sentinel written" || say FAIL "failed sentinel written"

echo "4b. prep sync FAILURE is diagnosable: nonzero exit vs timeout, with command output"
WS4B="$TMP/ws4b"; mkws "$WS4B"
GR_WS="$WS4B" GR_STEP_TIMEOUT=2 GR_SYNC_CMD="printf 'boom-detail\n' >&2; exit 7" \
  bash "$REPO/src/agent/restart-prep.sh" t4b >/dev/null 2>&1
r4b="$(cat "$WS4B/state/restart-prep-failed.json" 2>/dev/null || echo '')"
case "$r4b" in
  *"exited 7"*)     say ok "fast failure reports its exit status" ;;
  *)                say FAIL "fast failure reports its exit status: $r4b" ;;
esac
case "$r4b" in
  *boom-detail*)    say ok "fast failure preserves command output" ;;
  *)                say FAIL "fast failure preserves command output: $r4b" ;;
esac
case "$r4b" in
  *"timed out"*)    say FAIL "fast failure must NOT claim a timeout: $r4b" ;;
  *)                say ok "fast failure does not claim a timeout" ;;
esac

WS4C="$TMP/ws4c"; mkws "$WS4C"
GR_WS="$WS4C" GR_STEP_TIMEOUT=1 GR_SYNC_CMD="sleep 5" bash "$REPO/src/agent/restart-prep.sh" t4c >/dev/null 2>&1
r4c="$(cat "$WS4C/state/restart-prep-failed.json" 2>/dev/null || echo '')"
case "$r4c" in
  *"timed out after 1s"*) say ok "timeout says so, with the bound" ;;
  *)                      say FAIL "timeout says so, with the bound: $r4c" ;;
esac
case "$r4c" in
  *"exited "*)      say FAIL "timeout must NOT be reported as an ordinary exit: $r4c" ;;
  *)                say ok "timeout is not reported as an ordinary exit" ;;
esac

echo "5. DEAD core (stale .alive) → no wait; restart even if prep fails"
WS5="$TMP/ws5"; mkws "$WS5"
rm -f "$WS5/state/cores/$HOST.alive"
printf '{"status":"running","step":"ghost","ts":%s}\n' "$(date +%s)" > "$WS5/state/core-status.json"
out="$(GR_WS="$WS5" GR_SYNC_CMD="false" GR_POLL_S=1 bash "$GR" --dry-run 2>&1)"; rc=$?
[ "$rc" = "$DRY_OK" ] && say ok "exit $DRY_OK (dry run)" || say FAIL "exit $DRY_OK (got $rc): $out"
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

echo "7b. a TERM-IGNORING sync is still bounded, and reports a timeout"
# TERM alone never reaps this child; without the KILL escalation the bound is
# advertised only and prep reports an on-time success after the full runtime.
WS7B="$TMP/ws7b"; mkdir -p "$WS7B/state" "$WS7B/tasks"
t0=$(date +%s)
GR_WS="$WS7B" GR_STEP_TIMEOUT=1 GR_KILL_GRACE_S=2 \
  GR_SYNC_CMD='trap "" TERM; sleep 30' \
  bash "$REPO/src/agent/restart-prep.sh" termignore-rid >/dev/null 2>&1; rc=$?
elapsed=$(( $(date +%s) - t0 ))
[ "$elapsed" -le 8 ] \
  && say ok "TERM-ignoring sync bounded in ${elapsed}s (bound 1 + grace 2)" \
  || say FAIL "TERM-ignoring sync ran ${elapsed}s — the bound is advertised, not enforced"
[ "$rc" != 0 ] && say ok "prep reports failure (exit $rc), not an on-time success" \
  || say FAIL "prep exited 0 after ignoring its own timeout"
[ ! -f "$WS7B/state/restart-ready.json" ] \
  && say ok "no ready sentinel written for a timed-out sync" \
  || say FAIL "ready sentinel written despite the sync never completing"
grep -q 'timed out after' "$WS7B/state/restart-prep-failed.json" 2>/dev/null \
  && say ok "failed sentinel names the timeout" \
  || say FAIL "failed sentinel missing or does not name the timeout"

echo "7c. a timed-out sync's DESCENDANTS are reaped, not just the direct child"
# A pid-only kill leaves the grandchild running, so prep reports "timed out" and the
# restart proceeds while that process is still writing to the workspace.
WS7C="$TMP/ws7c"; mkdir -p "$WS7C/state" "$WS7C/tasks"
GR_WS="$WS7C" GR_STEP_TIMEOUT=1 GR_KILL_GRACE_S=2 \
  GR_SYNC_CMD='trap "" TERM; sleep 41 & echo $! >"'"$WS7C"'/gchild.pid"; wait' \
  bash "$REPO/src/agent/restart-prep.sh" pgroup-rid >/dev/null 2>&1
gpid="$(cat "$WS7C/gchild.pid" 2>/dev/null)"
# Fixture control: with no grandchild the survival check below passes vacuously.
[ -n "$gpid" ] && say ok "fixture spawned a grandchild (pid $gpid)" \
  || say FAIL "no grandchild pid recorded — fixture is unrepresentative, not passing"
sleep 1
if [ -n "$gpid" ] && kill -0 "$gpid" 2>/dev/null; then
  kill -KILL "$gpid" 2>/dev/null
  say FAIL "grandchild $gpid SURVIVED — signals went to the pid, not the process group"
else
  say ok "grandchild reaped with its process group"
fi

echo

# --- GNU-style `stat -f` must not break alive_age() ------------------------------------

# GNU `stat -f` is --file-system: it dumps and EXITS 0, so selecting on exit
# status never reaches `stat -c` and every core reads DEAD.
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
  # Return the REAL mtime without hardcoding either syntax: the host's own stat
  # may be GNU or BSD, so a hardcoded flag yields nothing on the other.
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
# Load-bearing: a seconds-old heartbeat must not read as dead. Only output-shape
# selection passes; BSD-only and digit-guard-only both fail here.
echo "$out" | grep -q "core is DEAD" \
  && say FAIL "fresh .alive must NOT read as DEAD under GNU-shaped stat: $out" \
  || say ok "fresh .alive still reads fresh under GNU-shaped stat"
echo "$out" | grep -qiE 'integer expression|unbound variable|File:' \
  && say FAIL "no arithmetic/unbound diagnostic leaked: $out" \
  || say ok "no arithmetic/unbound diagnostic"
[ "$rc" = "$DRY_OK" ] && say ok "exit $DRY_OK under GNU-style stat" || say FAIL "exit $DRY_OK under GNU-style stat (got $rc): $out"

echo "9. A LIVE holder waiting longer than LOCK_STALE_S must NOT be reaped"
# The gate is unbounded but `mkdir` stamps $LOCKDIR's mtime ONCE, so a holder
# waiting past LOCK_STALE_S can be reaped alive. Wait (5s) exceeds it (2s).
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

echo "10. A holder that LOSES its lease must defer, not restart alongside the reaper"
# A stalls past LOCK_STALE_S (SIGSTOP models a scheduler stall), B reaps and
# acquires, A resumes. Exactly one may decide.
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
# Barriers on $LOCKDIR/rid — the value own_lock() compares — not fixed sleeps.
# Unverified sleeps made this case UNDECIDABLE: if A had not yet acquired, or B
# had not yet reaped, the lease-loss scenario never occurred and the assertion
# still printed "N restart decisions", identical to a real concurrent restart.
LOCK10="$WS10/state/locks/graceful-restart.lock"
B=""   # set -u: the setup-failure path still reaches `wait "$B"`
rid_A=""
for _ in $(seq 1 100); do
  rid_A="$(cat "$LOCK10/rid" 2>/dev/null || echo '')"
  [ -n "$rid_A" ] && break
  sleep 0.1
done
if [ -z "$rid_A" ]; then
  say FAIL "case 10 SETUP: A never acquired the lease — scenario not exercised (not a concurrency bug)"
else
  kill -STOP "$A" 2>/dev/null  # stall A past the stale threshold
  sleep 4                      # real wait: must exceed GR_LOCK_STALE_S=2 so B reaps
  ( GR_WS="$WS10" GR_SYNC_CMD="true" GR_POLL_S=1 GR_LOCK_STALE_S=2 bash "$GR" --dry-run \
      >"$TMP/ws10_B.out" 2>&1 ) &
  B=$!
  rid_B=""
  for _ in $(seq 1 100); do
    rid_B="$(cat "$LOCK10/rid" 2>/dev/null || echo '')"
    [ -n "$rid_B" ] && [ "$rid_B" != "$rid_A" ] && break
    sleep 0.1
  done
  [ -n "$rid_B" ] && [ "$rid_B" != "$rid_A" ] \
    || say FAIL "case 10 SETUP: B never took the lease from A — scenario not exercised (not a concurrency bug)"
  kill -CONT "$A" 2>/dev/null  # A resumes holding a lease it no longer owns
fi
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
# cleanup_lock's own status must not become the script's: on lease-loss the lock
# is not ours, so a bare `own_lock && rm -rf` turns a documented exit 4 into 1.
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
# Two failures meet here: set -e aborts the TERM trap before `exit 143`, and the
# re-fired EXIT trap runs cleanup_lock again and overrides 143 with 1.
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
# Verify-then-fire is the natural operator sequence, so a dry-run that keeps the
# lock blocks the real run. The concurrency model opts into retention explicitly.
WS13="$TMP/ws13"; mkws "$WS13"
out13a="$(GR_WS="$WS13" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run 2>&1 || true)"
echo "$out13a" | grep -q "DRY-RUN — would exec" || say FAIL "dry-run did not reach a restart decision: $out13a"
LOCK13="$WS13/state/locks/graceful-restart.lock"
[ ! -d "$LOCK13" ] && say ok "a decided dry-run released its own lock" \
  || say FAIL "dry-run retained the lock — the next real run self-blocks for LOCK_STALE_S"

# The load-bearing assertion: the FOLLOWING run must not defer.
out13b="$(GR_WS="$WS13" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run 2>&1)"; rc13b=$?
[ "$rc13b" = "$DRY_OK" ] && say ok "the run immediately after a dry-run proceeds (rc $DRY_OK)" \
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

echo "14. trailing args are forwarded into the exec argv (menu-bar --visible)"
# Reads the argv back rather than asserting the flag was "passed". The negative
# control matters: a script that hardcoded --visible would pass without it.
WS14x="$TMP/ws13"; mkws "$WS14x"
out14="$(GR_WS="$WS14x" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run -- --visible 2>&1 || true)"
echo "$out14" | grep -q "would exec 'start-cli.sh --restart --visible'" \
  && say ok "--visible forwarded through to the exec argv" \
  || say FAIL "--visible was NOT forwarded: $(echo "$out14" | grep 'would exec' | cut -c1-160)"

WS15b="$TMP/ws13b"; mkws "$WS15b"
out14b="$(GR_WS="$WS15b" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run 2>&1 || true)"
echo "$out14b" | grep -q "would exec 'start-cli.sh --restart'" \
  && say ok "no trailing args → bare --restart (negative control)" \
  || say FAIL "bare invocation did not produce a bare --restart argv: $(echo "$out14b" | grep 'would exec' | cut -c1-160)"

echo "14b. a dry run must NOT exit 0 — the caller cannot tell it from a real restart"
# The menu-bar app maps the status to a user-facing notification, and every
# exit 0 read as "Core restarted". A rehearsal killed nothing, so it needs
# its own code. Real-restart control below keeps this from passing vacuously.
WS14c="$TMP/ws14c"; mkws "$WS14c"
GR_WS="$WS14c" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run -- --visible >/dev/null 2>&1
rc14c=$?
[ "$rc14c" = 5 ] \
  && say ok "dry run exits 5 (nothing killed or restarted)" \
  || say FAIL "dry run exited $rc14c, want 5 — a caller cannot distinguish it from a real restart"

WS14d="$TMP/ws14d"; mkws "$WS14d"
stub14d="$TMP/stub-start-cli-14d.sh"
printf '#!/bin/bash\nexit 0\n' > "$stub14d"; chmod +x "$stub14d"
GR_WS="$WS14d" GR_SYNC_CMD="true" GR_POLL_S=1 GR_START_CLI="$stub14d" bash "$GR" -- --visible >/dev/null 2>&1
rc14d=$?
[ "$rc14d" = 0 ] \
  && say ok "a REAL restart still exits 0 (control: 5 is not simply the new always)" \
  || say FAIL "real restart exited $rc14d, want 0"

echo "15. the REAL exec argv forwards the flag (not just the dry-run echo)"
# Case 14 reads the dry-run log, and dry-run returns BEFORE the exec, so it
# cannot see the production argv. This stubs GR_START_CLI and asserts that.
WS15="$TMP/ws15"; mkws "$WS15"
stub15="$TMP/stub-start-cli.sh"
cat > "$stub15" <<'STUB'
#!/bin/bash
printf '%s\n' "$@" > "$STUB_ARGV_OUT"
STUB
chmod +x "$stub15"
STUB_ARGV_OUT="$TMP/ws15.argv" GR_START_CLI="$stub15" GR_WS="$WS15" GR_SYNC_CMD="true" \
  GR_POLL_S=1 bash "$GR" -- --visible > "$TMP/ws15.out" 2>&1 || true
argv15="$(tr '\n' ' ' < "$TMP/ws15.argv" 2>/dev/null || echo MISSING)"
case "$argv15" in
  *"--restart"*"--visible"*) say ok "real exec argv carried --restart --visible ($argv15)" ;;
  *)                         say FAIL "real exec argv lost the flag: '$argv15'" ;;
esac

WS15b="$TMP/ws15b"; mkws "$WS15b"
STUB_ARGV_OUT="$TMP/ws15b.argv" GR_START_CLI="$stub15" GR_WS="$WS15b" GR_SYNC_CMD="true" \
  GR_POLL_S=1 bash "$GR" > "$TMP/ws15b.out" 2>&1 || true
argv15b="$(tr -d '\n' < "$TMP/ws15b.argv" 2>/dev/null || echo MISSING)"
[ "$argv15b" = "--restart" ] && say ok "bare invocation execs exactly --restart (negative control)" \
  || say FAIL "bare invocation argv was '$argv15b', expected exactly '--restart'"

echo "16. TERM during the wait must produce ZERO restart decisions (nudge->force = exactly one)"
# The nudge points at Force Restart while a waiter is still in the gate: if TERM
# leaves it able to reach the exec, force and waiter both restart the core.
WS16="$TMP/ws16"; mkws "$WS16"
printf '{"status":"running","step":"busy","ts":%s}\n' "$(date +%s)" > "$WS16/state/core-status.json"
stub16="$TMP/stub16.sh"
cat > "$stub16" <<'STUB'
#!/bin/bash
printf '%s\n' "$@" >> "$STUB_ARGV_OUT"
STUB
chmod +x "$stub16"
: > "$TMP/ws16.argv"
( STUB_ARGV_OUT="$TMP/ws16.argv" GR_START_CLI="$stub16" GR_WS="$WS16" GR_SYNC_CMD="true" \
    GR_POLL_S=1 bash "$GR" -- --visible > "$TMP/ws16.out" 2>&1; echo $? > "$TMP/ws16.rc" ) &
w16=$!
LOCK16="$WS16/state/locks/graceful-restart.lock"
for _ in $(seq 1 60); do [ -f "$LOCK16/rid" ] && break; sleep 0.2; done
gr16="$(pgrep -P "$w16" -f "graceful-restart.sh" | head -1)"
[ -n "$gr16" ] && kill -TERM "$gr16" 2>/dev/null
# Simulate the force making the core un-busy, which is what used to let the
# surviving waiter proceed.
mv "$WS16/state/core-status.json" "$WS16/state/core-status.json.aside" 2>/dev/null
wait "$w16" 2>/dev/null || true
sleep 1
# Count via wc, not `grep -c || echo 0`: grep -c on an empty file prints 0 AND
# exits 1, so the fallback also fires and the value becomes "0\n0".
decisions16=$(grep -- "--restart" "$TMP/ws16.argv" 2>/dev/null | wc -l | tr -d ' ')
[ "$decisions16" = 0 ] \
  && say ok "TERM'd waiter reached NO exec (0 restart decisions), so force+waiter cannot double" \
  || say FAIL "$decisions16 restart decision(s) survived TERM — force + waiter would restart twice"
[ ! -d "$LOCK16" ] && say ok "TERM'd waiter released the lock, so the forced replacement won't defer" \
  || say FAIL "lock survived TERM — a forced restart would hit exit 4"
echo "17. an EMPTY core-status.json is the truncate WINDOW, not idle — re-read, do not kill through it"
# Every writer is a `>` truncate-then-write, so a poll can land between the two.
# Modelled deterministically: empty now, "running" well inside the re-read budget.
# Write well inside the 5x50ms re-read budget. A loaded runner can stretch
# either side, so keep the margin visible and overridable rather than implicit.
GR_T17_WRITE_DELAY="${GR_T17_WRITE_DELAY:-0.10}"
WS17="$TMP/ws17"; mkws "$WS17"
: > "$WS17/state/core-status.json"          # the window: readable, empty
( for _ in $(seq 1 40); do touch "$WS17/state/cores/$HOST.alive"; sleep 0.5; done ) &
keeper17=$!
( sleep "$GR_T17_WRITE_DELAY"
  printf '{"status":"running","ts":%s}\n' "$(date +%s)" > "$WS17/state/core-status.json" ) &
writer17=$!
( GR_WS="$WS17" GR_SYNC_CMD="true" GR_POLL_S=1 GR_STATUS_REREADS=5 bash "$GR" --dry-run \
    > "$TMP/ws17.out" 2>&1 ) &
gr17=$!
wait "$writer17" 2>/dev/null
sleep 3
if kill -0 "$gr17" 2>/dev/null; then
  say ok "the gate re-read the window and stayed in the busy wait"
else
  say FAIL "the gate decided through the truncate window: $(grep -h 'would exec' "$TMP/ws17.out" | head -1)"
fi
printf '{"status":"idle","ts":%s}\n' "$(date +%s)" > "$WS17/state/core-status.json"
# Bounded, not `wait`: a gate that never returns must fail AS case 17, not as an
# anonymous job timeout — the attribution defect case 10 exists to prevent.
gr17_done=0
for _ in $(seq 1 60); do
  kill -0 "$gr17" 2>/dev/null || { gr17_done=1; break; }
  sleep 0.5
done
if [ "$gr17_done" = 0 ]; then
  kill -9 "$gr17" 2>/dev/null || true
  say FAIL "case 17: the gate never returned within 30s of a readable idle status — hung, not merely slow"
fi
wait "$gr17" 2>/dev/null
kill "$keeper17" 2>/dev/null || true; wait "$keeper17" 2>/dev/null || true
grep -q "would exec" "$TMP/ws17.out" \
  && say ok "and it proceeded once the core reported idle" \
  || say FAIL "the gate never proceeded on a readable idle status — over-corrected into a hang"

echo "18. an ABSENT core-status.json still reads as not-busy (no regression)"
WS18="$TMP/ws18"; mkws "$WS18"
rm -f "$WS18/state/core-status.json"
touch "$WS18/state/cores/$HOST.alive"
( for _ in $(seq 1 20); do touch "$WS18/state/cores/$HOST.alive"; sleep 0.5; done ) &
k18=$!
GR_WS="$WS18" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run > "$TMP/ws18.out" 2>&1 || true
kill "$k18" 2>/dev/null || true; wait "$k18" 2>/dev/null || true
grep -q "would exec" "$TMP/ws18.out" \
  && say ok "absent status = nothing running = proceed" \
  || say FAIL "absent status now blocks the restart — the empty-read fix over-reached"

echo "19. the phase stream is PERSISTED to logs/graceful-restart.log (the app pipes stdout only into itself)"
WS19="$TMP/ws19"; mkws "$WS19"
out="$(GR_WS="$WS19" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run 2>&1)"
rid="$(printf '%s\n' "$out" | sed -n 's/^graceful-restart\[\(grp-[0-9]*-[0-9]*\)\].*/\1/p' | head -1)"
LOG19="$WS19/logs/graceful-restart.log"
[ -f "$LOG19" ] \
  && say ok "log file created under the workspace" \
  || say FAIL "no $LOG19 — the stream still lives only in the caller's pipe"
[ -n "$rid" ] && grep -q "graceful-restart\[$rid\]: DRY-RUN — would exec" "$LOG19" 2>/dev/null \
  && say ok "the decision line landed on disk, scoped to this run's RID ($rid)" \
  || say FAIL "decision line for rid='$rid' missing from the log"
grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z graceful-restart\[' "$LOG19" 2>/dev/null \
  && say ok "persisted lines are UTC-timestamped" \
  || say FAIL "persisted lines carry no timestamp"
# stdout is a contract with main.swift's phase matcher: no timestamp may leak into it.
if printf '%s\n' "$out" | grep -q '^graceful-restart\[' && ! printf '%s\n' "$out" | grep -q '^[0-9]\{4\}-[0-9]\{2\}-'; then
  say ok "stdout shape unchanged (no timestamp prefix in the app's phase stream)"
else
  say FAIL "stdout shape changed — main.swift's restartPhaseMessage matcher may break"
fi
n_out="$(printf '%s\n' "$out" | grep -c '^graceful-restart\[')"
n_log="$(grep -c "graceful-restart\[$rid\]" "$LOG19" 2>/dev/null || echo 0)"
[ "$n_out" -gt 0 ] && [ "$n_out" = "$n_log" ] \
  && say ok "every stdout phase line ($n_out) has a disk twin" \
  || say FAIL "stdout has $n_out phase lines, disk has $n_log — one side is missing a line"
GR_WS="$WS19" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run >/dev/null 2>&1
grep -q "graceful-restart\[$rid\]" "$LOG19" \
  && say ok "a later run APPENDS — the earlier run's trace survives" \
  || say FAIL "the second run truncated the first run's trace"
echo "20. a LIVE core is handed a drain TASK before the gate; it is retired before the exec"
# Owner rule 2026-09-03: "a busy core may never read the task" justifies the .alive
# fallback, not skipping the task. A real run (stub launcher) so the write is visible.
WS20="$TMP/ws20"; mkws "$WS20"
printf '{"status":"running","step":"x","ts":%s}\n' "$(date +%s)" > "$WS20/state/core-status.json"
stub20="$TMP/stub20-start-cli.sh"
cat > "$stub20" <<'STUB'
#!/bin/bash
printf '%s\n' "$@" > "$STUB_ARGV_OUT"
STUB
chmod +x "$stub20"
# A fake core: stays busy until the drain task appears, keeps a copy, then goes idle.
( for _ in $(seq 1 40); do
    f="$(ls "$WS20"/tasks/task-restart-prep-*.txt 2>/dev/null | head -1)"
    if [ -n "$f" ]; then cp "$f" "$TMP/ws20.task"; break; fi
    touch "$WS20/state/cores/$HOST.alive"; sleep 0.5
  done
  printf '{"status":"idle","ts":%s}\n' "$(date +%s)" > "$WS20/state/core-status.json" ) &
core20=$!
STUB_ARGV_OUT="$TMP/ws20.argv" GR_START_CLI="$stub20" GR_WS="$WS20" GR_SYNC_CMD="true" \
  GR_POLL_S=1 bash "$GR" > "$TMP/ws20.out" 2>&1 || true
wait "$core20" 2>/dev/null
rid20="$(sed -n 's/^graceful-restart\[\(grp-[0-9]*-[0-9]*\)\].*/\1/p' "$TMP/ws20.out" | head -1)"
[ -s "$TMP/ws20.task" ] \
  && say ok "the core received a drain task while the gate was waiting" \
  || say FAIL "no drain task reached the core (gate waited on status alone)"
grep -q "^id: task-restart-prep-$rid20$" "$TMP/ws20.task" 2>/dev/null \
  && say ok "task id is scoped to this run's RID ($rid20)" \
  || say FAIL "task id not scoped to $rid20: $(head -1 "$TMP/ws20.task" 2>/dev/null)"
grep -q "^priority: urgent$" "$TMP/ws20.task" 2>/dev/null && grep -q "^access_tier: owner$" "$TMP/ws20.task" 2>/dev/null \
  && say ok "urgent + owner-tier, so the queue serves it first with full capability" \
  || say FAIL "task headers wrong: $(grep -E '^(priority|access_tier):' "$TMP/ws20.task" 2>/dev/null | tr '\n' ' ')"
grep -q "^task: RESTART_PREP: $rid20" "$TMP/ws20.task" 2>/dev/null && grep -q "task:.*core-status.sh idle" "$TMP/ws20.task" \
  && say ok "the body tells the core the one action that opens the gate (status idle)" \
  || say FAIL "task body does not carry the RESTART_PREP contract"
grep -q "restart" "$TMP/ws20.argv" 2>/dev/null \
  && say ok "and the restart proceeded once the core went idle" \
  || say FAIL "the launcher stub was never exec'd"
[ -f "$WS20/tasks/archive/task-restart-prep-$rid20.txt" ] && [ ! -f "$WS20/tasks/task-restart-prep-$rid20.txt" ] \
  && say ok "the drain task was retired to tasks/archive/ before the exec (no orphan for the next boot)" \
  || say FAIL "drain task not retired: live=$(ls "$WS20"/tasks/task-restart-prep-* 2>/dev/null) archive=$(ls "$WS20"/tasks/archive/ 2>/dev/null)"

WS20b="$TMP/ws20b"; mkws "$WS20b"; rm -f "$WS20b/state/cores/$HOST.alive"   # DEAD core
STUB_ARGV_OUT="$TMP/ws20b.argv" GR_START_CLI="$stub20" GR_WS="$WS20b" GR_SYNC_CMD="true" \
  GR_POLL_S=1 bash "$GR" > "$TMP/ws20b.out" 2>&1 || true
[ -z "$(ls "$WS20b"/tasks/task-restart-prep-* "$WS20b"/tasks/archive/task-restart-prep-* 2>/dev/null)" ] \
  && say ok "CONTROL: a DEAD core gets no drain task (nobody would read it)" \
  || say FAIL "a drain task was written for a dead core"

WS20c="$TMP/ws20c"; mkws "$WS20c"
out20c="$(GR_WS="$WS20c" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" --dry-run 2>&1)"
[ -z "$(ls "$WS20c"/tasks/task-restart-prep-* 2>/dev/null)" ] && echo "$out20c" | grep -q "would write the drain task" \
  && say ok "CONTROL: a dry run names the task it would write and writes nothing (a rehearsal must not drain a live core)" \
  || say FAIL "dry run wrote a task or did not say so"

WS20d="$TMP/ws20d"; mkws "$WS20d"
printf '{"status":"running","step":"x","ts":%s}\n' "$(date +%s)" > "$WS20d/state/core-status.json"
( sleep 2; printf '{"status":"idle","ts":%s}\n' "$(date +%s)" > "$WS20d/state/core-status.json" ) &
flip20d=$!
GR_WS="$WS20d" GR_SYNC_CMD="false" GR_POLL_S=1 bash "$GR" > "$TMP/ws20d.out" 2>&1; rc20d=$?
wait "$flip20d" 2>/dev/null
if [ "$rc20d" = 3 ] && [ -z "$(ls "$WS20d"/tasks/task-restart-prep-* 2>/dev/null)" ]; then
  say ok "prep FAILURE (exit 3) still retires the drain task — the core is not left told to stay idle"
else
  say FAIL "exit $rc20d and live task(s): $(ls "$WS20d"/tasks/ 2>/dev/null | tr '\n' ' ')"
fi

echo "20e. everything cleanup_lock calls is DEFINED before the traps that call it are armed"
# yixuan-ag2 on #3823: retire_prep_task was defined 60 lines after `trap cleanup_lock EXIT`,
# so a TERM in that window exited 127 (command not found) instead of 143.
def_line="$(grep -n '^retire_prep_task()' "$GR" | head -1 | cut -d: -f1)"
trap_line="$(grep -n '^trap cleanup_lock EXIT' "$GR" | head -1 | cut -d: -f1)"
pt_line="$(grep -n '^PREP_TASK=' "$GR" | head -1 | cut -d: -f1)"
if [ -n "$def_line" ] && [ -n "$trap_line" ] && [ "$def_line" -lt "$trap_line" ] && [ "$pt_line" -lt "$trap_line" ]; then
  say ok "retire_prep_task (line $def_line) and PREP_TASK (line $pt_line) precede the EXIT trap (line $trap_line)"
else
  say FAIL "ordering: retire_prep_task=$def_line PREP_TASK=$pt_line trap=$trap_line — a TERM before the definition exits 127"
fi
# behavioural control: a TERM delivered as early as bash allows still exits 143 and leaves no task behind
WS20e="$TMP/ws20e"; mkws "$WS20e"
printf '{"status":"running","step":"x","ts":%s}\n' "$(date +%s)" > "$WS20e/state/core-status.json"
GR_WS="$WS20e" GR_SYNC_CMD="true" GR_POLL_S=1 bash "$GR" > "$TMP/ws20e.out" 2>&1 &
gr20e=$!
sleep 0.3; kill -TERM "$gr20e" 2>/dev/null; wait "$gr20e" 2>/dev/null; rc20e=$?
[ "$rc20e" = 143 ] && ! grep -q "command not found" "$TMP/ws20e.out" \
  && say ok "TERM 0.3s in -> exit 143, no 'command not found'" \
  || say FAIL "early TERM -> exit $rc20e: $(grep -i 'not found' "$TMP/ws20e.out" | head -1)"

if [ "$fails" = 0 ]; then
  echo "ALL PASS"
else
  echo "$fails FAILURE(S)"
  exit 1
fi
