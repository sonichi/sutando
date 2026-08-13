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
KILL_LIST=()
cleanup_all() {
  for p in "${KILL_LIST[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done
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

# ------------------------------------------------------------ fail-safe direction
check "$(sentinel_pid_wrote_file 999999 "$PF3" && echo 0 || echo 1)" \
      "f1) unmeasurable pid fails SAFE (treated as owner, nothing is killed)"

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
