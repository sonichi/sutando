#!/usr/bin/env bash
# The lane scheduler hands the next suite to whichever worker is free. With a
# fixed stride, worker 1 would own lines 1 and 3, so line 3 could not start
# until line 1 finished; here line 1 blocks on a gate and line 3 must still
# finish before that gate opens. Drives the SHIPPED scheduler, not a copy.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LANE="$here/scripts/parallel-suite-lane.sh"
T="$(mktemp -d "${TMPDIR:-/tmp}/lane-queue-XXXXXX")"
trap 'rm -rf "$T"' EXIT
fail=0
GATE="$T/gate"

# Suite 1 blocks until the gate exists; suites 2 and 3 finish at once. Absolute
# paths, because each worker runs its suite from inside its own worktree.
printf 'until [ -e "%s" ]; do sleep 0.05; done\n' "$GATE" > "$T/s1.sh"
printf 'exit 0\n' > "$T/s2.sh"
printf 'exit 0\n' > "$T/s3.sh"
printf '%s\n%s\n%s\n' "$T/s1.sh" "$T/s2.sh" "$T/s3.sh" > "$T/files"
mkdir -p "$T/rec"

( cd "$here" && bash "$LANE" 2 "$T/files" "$T/rec" bash ) & lane_pid=$!

# Line 3 must be recorded while line 1 is still blocked — the free worker took it.
_tries=0
while [ ! -f "$T/rec/3.rc" ] && [ "$_tries" -lt 200 ]; do sleep 0.05; _tries=$((_tries + 1)); done
if [ -f "$T/rec/3.rc" ] && [ ! -f "$T/rec/1.rc" ]; then
    echo "  ok   line 3 ran on the free worker while line 1 was still blocked"
else
    echo "  FAIL line 3 waited behind line 1 (3.rc=$([ -f "$T/rec/3.rc" ] && echo present || echo absent), 1.rc=$([ -f "$T/rec/1.rc" ] && echo present || echo absent))"; fail=1
fi

: > "$GATE"
wait "$lane_pid"

# Every line ran exactly once, each with its own record and a zero exit.
for i in 1 2 3; do
    if [ "$(cat "$T/rec/$i.rc" 2>/dev/null)" = "0" ]; then
        echo "  ok   line $i recorded rc=0"
    else
        echo "  FAIL line $i rc=$(cat "$T/rec/$i.rc" 2>/dev/null || echo missing)"; fail=1
    fi
done
_n=0; for _r in "$T/rec"/*.rc; do [ -e "$_r" ] && _n=$((_n + 1)); done
if [ "$_n" = 3 ]; then
    echo "  ok   exactly three records"
else
    echo "  FAIL record count $_n: $(ls "$T/rec")"; fail=1
fi
# The claim markers are scheduler bookkeeping and leave with the scheduler.
if ls -d "$T/rec"/.claim-* >/dev/null 2>&1; then
    echo "  FAIL claim markers left behind: $(ls -d "$T/rec"/.claim-*)"; fail=1
else
    echo "  ok   claim markers cleaned up"
fi

if [ "$fail" = 0 ]; then
    echo "parallel-lane-dynamic-queue: all checks pass"
else
    echo "parallel-lane-dynamic-queue: FAILED"; exit 1
fi
