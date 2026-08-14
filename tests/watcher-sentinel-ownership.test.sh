#!/usr/bin/env bash
# Concurrency + ownership contract for src/watcher_sentinel.sh.
#
# Every case drives the PRODUCTION functions — the shared helper and the real
# reap_stale_task_watcher — not a re-implementation of their recipe. A copied
# recipe would pass while the shipped code kept the bug, which is the exact
# shape this file exists to prevent.
#
# Run: bash tests/watcher-sentinel-ownership.test.sh
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../src/watcher_sentinel.sh
source "$REPO/src/watcher_sentinel.sh"
# shellcheck source=../src/startup-runtime.sh
source "$REPO/src/startup-runtime.sh" >/dev/null 2>&1 || true

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok   $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL $1"; }
check(){ if [ "$1" = "0" ]; then ok "$2"; else bad "$2"; fi; }

TMP="$(mktemp -d)"
PIDFILE="$TMP/spawned.pids"
KILL_LIST=()
cleanup_all() {
  for p in "${KILL_LIST[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done
  # KILL_LIST cannot see a pid appended inside $( ): spawn_fake_watcher is always
  # called as PID="$(spawn_fake_watcher ...)", so the += lands in the subshell's
  # copy and the parent array stays empty. The fakes the reaper is SUPPOSED to
  # leave alive therefore survived the suite as ppid=1 orphans for their full
  # sleep, and health-check's task-watcher probe counts them as live watchers.
  [ -f "$PIDFILE" ] && while read -r p; do [ -n "$p" ] && kill "$p" 2>/dev/null; done < "$PIDFILE"
  rm -rf "$TMP"
}
trap cleanup_all EXIT

# A real process whose argv matches the reaper's `ps ... | grep watch-tasks-stream`.
spawn_fake_watcher() {
  local dir="$1" script="$1/watch-tasks-stream.sh"
  printf '#!/usr/bin/env bash\nsleep 120\n' > "$script"
  chmod +x "$script"
  # stdout/stderr MUST be redirected: this runs inside $( ), and a background
  # child inheriting that pipe keeps it open, so the substitution would block
  # until the child exits — a 120s hang rather than a test.
  "$script" >/dev/null 2>&1 & local pid=$!
  KILL_LIST+=("$pid")
  echo "$pid" >> "$PIDFILE"   # survives the $( ) the caller wraps this in
  # Wait until ps can actually see it, so the test never races the fixture.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    ps -p "$pid" -o args= 2>/dev/null | grep -q "watch-tasks-stream" && break
    sleep 0.1
  done
  printf '%s' "$pid"
}

echo "watcher-sentinel ownership:"

# ---------------------------------------------------------------- elapsed parse
E=$(printf '' ; echo "01:02" | awk -F'[-:]' '{if(NF==2)print ($1*60)+$2}')
check "$([ "$E" = "62" ] && echo 0 || echo 1)" "e1) MM:SS parses to seconds"

# ------------------------------------------------- the reviewer's same-value ABA
# A LIVE watcher owns the sentinel under a REISSUED pid: the file predates the
# process. Baseline deleted it (and signalled the process); it must now survive.
D1="$TMP/aba"; mkdir -p "$D1"
PID1="$(spawn_fake_watcher "$D1")"
PF1="$D1/watch.pid"
printf '%s\n' "$PID1" > "$PF1"
touch -t 202601010000 "$PF1"          # sentinel far older than the process

reap_stale_task_watcher "$PF1" >"$TMP/out1" 2>&1
check "$([ -f "$PF1" ] && echo 0 || echo 1)" \
      "a1) reissued pid: sentinel SURVIVES (was the deletion bug)"
check "$(ps -p "$PID1" >/dev/null 2>&1 && echo 0 || echo 1)" \
      "a2) reissued pid: the live watcher is NOT signalled"
check "$(grep -q 'reissued pid' "$TMP/out1" && echo 0 || echo 1)" \
      "a3) reissued pid: says why, rather than failing silently"

# ------------------------------------------------------- control: a REAL stale one
# Without this the case above proves only that the reaper stopped working.
D2="$TMP/stale"; mkdir -p "$D2"
PID2="$(spawn_fake_watcher "$D2")"
PF2="$D2/watch.pid"
printf '%s\n' "$PID2" > "$PF2"        # fresh sentinel: process is old enough to own it

reap_stale_task_watcher "$PF2" >"$TMP/out2" 2>&1
check "$([ -f "$PF2" ] && echo 1 || echo 0)" \
      "b1) genuine stale watcher: sentinel IS removed"
check "$(grep -q 'reaped stale' "$TMP/out2" && echo 0 || echo 1)" \
      "b2) genuine stale watcher: IS reaped"

# ------------------------------------------------ cleanup(): atomic release
D3="$TMP/rel"; mkdir -p "$D3"; PF3="$D3/watch.pid"

printf '4242\n' > "$PF3"
sentinel_release_if_owner "$PF3" 4242
check "$([ -f "$PF3" ] && echo 1 || echo 0)" "c1) owner releases its own sentinel"

printf '9999\n' > "$PF3"
sentinel_release_if_owner "$PF3" 4242
check "$([ -f "$PF3" ] && echo 0 || echo 1)" "c2) non-owner leaves it in place"
check "$([ "$(cat "$PF3")" = "9999" ] && echo 0 || echo 1)" "c3) non-owner does not alter it"

rm -f "$PF3"
sentinel_release_if_owner "$PF3" 4242
check "$([ -f "$PF3" ] && echo 1 || echo 0)" "c4) absent sentinel is a no-op, not an error"

# ------------------------------- cleanup(): a sentinel stamped MID-RELEASE survives
# The TOCTOU the claim closes: the old watcher is releasing while a new one
# stamps. Emulated by stamping at the moment the claim is held.
D4="$TMP/toctou"; mkdir -p "$D4"; PF4="$D4/watch.pid"
printf '4242\n' > "$PF4"
(
  # Wait for the claim to appear, then stamp a fresh sentinel at the free path.
  for _ in $(seq 1 200); do
    if ls "$PF4".claim.* >/dev/null 2>&1; then printf '7777\n' > "$PF4"; exit 0; fi
    sleep 0.01
  done
) & STAMPER=$!
sentinel_release_if_owner "$PF4" 4242
wait "$STAMPER" 2>/dev/null

if [ -f "$PF4" ]; then
  check "$([ "$(cat "$PF4")" = "7777" ] && echo 0 || echo 1)" \
        "d1) sentinel stamped during the claim is NOT destroyed"
else
  # The stamper may not have won the window; that is not a failure of the
  # contract, but say so rather than counting a pass we did not exercise.
  echo "  skip d1) stamper did not hit the claim window this run"
fi
check "$(ls "$PF4".claim.* >/dev/null 2>&1 && echo 1 || echo 0)" \
      "d2) no claim file is left behind"

# ------------------------------------------- portability: stat that SUCCEEDS wrongly
# GNU `stat -f` means FILESYSTEM status and succeeds with a human-readable block,
# so a BSD-first `||` chain never reaches the GNU form and $mtime becomes text.
# macOS cannot reproduce that natively — BSD `stat -f %m` is correct here — so
# the shape is forced with a stub. Without this, the bug is only findable on the
# ubuntu runner, which is exactly where it was found.
# Run in a SUBSHELL. Under the bug, `set -u` aborts the shell mid-arithmetic —
# in-line that killed this suite before its summary and still exited 0, so the
# guard would not have failed CI at all. Isolating it turns the abort into a
# non-zero rc this suite can actually assert on.
PF_S="$TMP/statshape.pid"; printf '%s\n' "$$" > "$PF_S"
(
  stat() { echo "  File: /some/path"; echo "    ID: 1234 Namelen: 255"; return 0; }
  sentinel_pid_wrote_file "$$" "$PF_S"
) >"$TMP/sout" 2>&1
SRC=$?
# rc alone no longer separates graceful from aborted: since the tri-state change,
# a graceful "cannot measure" is rc 2, and a `set -u` abort is also non-zero. So
# assert BOTH the exact UNKNOWN code and the absence of the abort's own message.
check "$([ "$SRC" -eq 2 ] && echo 0 || echo 1)" \
      "non-numeric stat output reports UNKNOWN (rc 2) instead of aborting (got $SRC)"
check "$(grep -qi "unbound variable" "$TMP/sout" && echo 1 || echo 0)" \
      "...and did not abort mid-arithmetic under set -u"
check "$(grep -qi 'unbound variable' "$TMP/sout" && echo 1 || echo 0)" \
      "and does not blow up on arithmetic with text"

# ------------------------------------- precondition: the sentinel is stamped IN PLACE
# A move-into-place writer preserves the old mtime, so the owner looks younger
# than its own sentinel and reaping stops entirely. Behavioural half first: show
# what a moved-in sentinel does to the verdict.
D5="$TMP/movedin"; mkdir -p "$D5"
PID5="$(spawn_fake_watcher "$D5")"
PF5="$D5/watch.pid"
printf '%s\n' "$PID5" > "$TMP/staged.pid"
touch -t 202601010000 "$TMP/staged.pid"     # built earlier, elsewhere
mv "$TMP/staged.pid" "$PF5"                 # mv preserves that old mtime
if sentinel_pid_wrote_file "$PID5" "$PF5"; then owned=0; else owned=1; fi
check "$([ "$owned" -eq 1 ] && echo 0 || echo 1)" \
      "p1) a moved-in sentinel reads as NOT written by its owner"
reap_stale_task_watcher "$PF5" >"$TMP/out5" 2>&1
check "$([ -f "$PF5" ] && echo 0 || echo 1)" \
      "p2) so the reaper stops cleaning it — reaping degrades to never"

# Shape half: only the WRITER decides how the file comes to exist, so no
# behavioural test can catch a future refactor to write-then-mv.
check "$(grep -qE '^echo "\$\$" > "\$PID_FILE"' "$REPO/src/watch-tasks-stream.sh" && echo 0 || echo 1)" \
      "p3) watch-tasks-stream.sh still stamps the sentinel in place"

# ------------------------------------------------------------ unmeasurable ownership
# rc 0 = wrote it, rc 1 = demonstrably did not, rc 2 = UNKNOWN. The old assertion
# here accepted rc 0 for an unmeasurable pid and called that "nothing is killed" —
# a claim about the REAPER that it never exercised. rc 0 is what makes the reaper
# kill, so f2 below drives the production reaper instead of the helper alone.
sentinel_pid_wrote_file 999999 "$PF3"; f1_rc=$?
check "$([ "$f1_rc" -eq 2 ] && echo 0 || echo 1)" \
      "f1) unmeasurable pid reports UNKNOWN (rc 2), not owner (got rc $f1_rc)"

# f2/f3 — the production reaper, forced through the unknown branch. Uses
# spawn_fake_watcher so the child's pipe handling matches every other case here.
DIR_U="$TMP/unknown"; mkdir -p "$DIR_U"
U_PID="$(spawn_fake_watcher "$DIR_U")"
PF_U="$DIR_U/watch-tasks-stream.pid"
echo "$U_PID" > "$PF_U"
_real_elapsed="$(declare -f sentinel_pid_elapsed)"
sentinel_pid_elapsed() { return 1; }          # ownership becomes unmeasurable
reap_stale_task_watcher "$PF_U" >"$TMP/outU" 2>&1
eval "$_real_elapsed"                          # restore for any later case
check "$(kill -0 "$U_PID" 2>/dev/null && echo 0 || echo 1)" \
      "f2) unmeasurable ownership does NOT kill the live watcher"
check "$([ -f "$PF_U" ] && echo 0 || echo 1)" \
      "f3) ...and does NOT unlink its sentinel"

# f4 — errexit. Production startup.sh runs under `set -e`, and a BARE call to the
# tri-state helper terminates the shell on rc 1/2 before either branch runs. Must
# be a separate `bash -e` process invoking the reaper as a simple command: calling
# it inside `cmd && ... || ...` disables errexit for the whole function body, which
# is how a first attempt at this test passed while production was broken.
for _rc in 0 1 2; do
  cat > "$TMP/errexit.sh" <<EOF
set -e
. "$REPO/src/watcher_sentinel.sh"
. "$REPO/src/startup-runtime.sh" 2>/dev/null || true
sentinel_pid_wrote_file() { return $_rc; }
ps() { echo "bash watch-tasks-stream.sh"; }
sentinel_release_if_owner() { :; }
kill() { :; }
PF="\$(mktemp)"; echo 99999 > "\$PF"
reap_stale_task_watcher "\$PF"
echo REACHED
EOF
  _out="$(bash "$TMP/errexit.sh" 2>&1)"
  check "$(printf '%s' "$_out" | grep -q REACHED && echo 0 || echo 1)" \
        "f4/$_rc) helper rc=$_rc does not abort the reaper under set -e"
done

# f5 — `ps` failing is not "not a watcher". A denied ps skipped the ownership
# check and still released the sentinel, deleting a live watcher's file.
cat > "$TMP/psfail.sh" <<EOF
. "$REPO/src/watcher_sentinel.sh"
. "$REPO/src/startup-runtime.sh" 2>/dev/null || true
ps() { echo "ps: Operation not permitted" >&2; return 1; }
OWN=0; REL=0
sentinel_pid_wrote_file() { OWN=1; return 2; }
sentinel_release_if_owner() { REL=1; }
PF="\$(mktemp)"; echo 99999 > "\$PF"
reap_stale_task_watcher "\$PF" >/dev/null 2>&1
echo "OWN=\$OWN REL=\$REL"
EOF
_ps="$(bash "$TMP/psfail.sh" 2>/dev/null)"
check "$(printf '%s' "$_ps" | grep -q 'REL=0' && echo 0 || echo 1)" \
      "f5) an unanswerable ps does NOT release the sentinel (got: $_ps)"

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
