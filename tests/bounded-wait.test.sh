#!/usr/bin/env bash
# wait_bounded (src/bounded-wait.sh): the one bound under the watcher's inline
# resolver and handler runs. Case 4 is the #5081 shape: the parent used to TERM
# its watchdog the instant the child exited, and on a loaded host that TERM
# reached a subshell that had not yet reset its inherited traps -- bash then
# dropped it (the parent sat out the whole bound) or ran the PARENT's trap in
# the child (the watcher's own cleanup killed the watcher mid-handler).
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../src/bounded-wait.sh
. "$REPO/src/bounded-wait.sh"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
# A private TMPDIR: the leftover scan below must not see a sibling suite's flags.
TMPDIR="$(mktemp -d)"; export TMPDIR
trap 'rm -rf "$TMPDIR"' EXIT
FLAG="$TMPDIR/bounded-wait-flag"

# 1. A child that finishes inside the bound: its own status, no flag.
( exit 7 ) &
wait_bounded $! 5 "$FLAG"; rc=$?
[ "$rc" = "7" ] && [ ! -e "$FLAG" ]
check $? "a child inside the bound returns its own status (rc=$rc) and raises no flag"

# 2. A child past the bound is TERMed, and the flag says the bound fired.
sleep 30 &
start=$(date +%s); wait_bounded $! 1 "$FLAG"; rc=$?; elapsed=$(( $(date +%s) - start ))
[ "$rc" = "143" ] && [ -e "$FLAG" ] && [ "$elapsed" -lt 10 ]
check $? "a child past the bound is TERMed (rc=$rc, ${elapsed}s) and the flag is raised"
rm -f "$FLAG"

# 3. A TERM-resistant child still dies: KILL follows TERM.
bash -c 'trap "" TERM; sleep 30' &
start=$(date +%s); wait_bounded $! 1 "$FLAG"; rc=$?; elapsed=$(( $(date +%s) - start ))
[ "$rc" = "137" ] && [ -e "$FLAG" ] && [ "$elapsed" -lt 10 ]
check $? "a TERM-resistant child is KILLed (rc=$rc, ${elapsed}s)"
rm -f "$FLAG"

# 4. The watchdog is stood down through the flag, never signalled (#5081): no
#    kill names it in the shipped source, and a finished child still returns
#    at once against a long bound -- the watchdog noticed the flag, not a TERM.
signals="$(grep -c 'kill[^#]*watchdog_pid' "$REPO/src/bounded-wait.sh")"
[ "$signals" = "0" ]
check $? "wait_bounded never signals its watchdog ($signals kill sites name it)"
( exit 0 ) &
start=$(date +%s); wait_bounded $! 3 "$FLAG"; rc=$?; elapsed=$(( $(date +%s) - start ))
[ "$rc" = "0" ] && [ ! -e "$FLAG" ] && [ "$elapsed" -lt 3 ]
check $? "a finished child returns within a tick of a 3s bound (rc=$rc, ${elapsed}s)"

# 5. Nothing is left behind: no done flag.
leftovers="$(find "$TMPDIR" -name 'sutando-bounded-done.*' | wc -l | tr -d ' ')"
[ "$leftovers" = "0" ]
check $? "no done flag is left behind ($leftovers)"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
