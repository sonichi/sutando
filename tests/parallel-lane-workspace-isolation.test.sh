#!/usr/bin/env bash
# Two state-mutating suites run concurrently must not share a resolved
# workspace. Baseline is REPEATED: the collision needs overlap and is flaky.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
A="tests/discord-bridge-reply-directive.test.py"
B="tests/dm-result-adoption-gap.test.py"
fail=0
TRIALS="${ISOLATION_TRIALS:-5}"

run_pair () {  # $1,$2 = cwd for each suite
    local f=0 i
    for ((i=0; i<TRIALS; i++)); do
        ( cd "$1" && python3 "$A" >/dev/null 2>&1 ) & local pid=$!
        sleep 0.12
        ( cd "$2" && python3 "$B" >/dev/null 2>&1 )
        wait $pid || f=$((f+1))
    done
    echo "$f"
}

wt="$(mktemp -d)"
git -C "$here" worktree add --detach -q "$wt/w" HEAD || { echo "  SKIP: no worktree support"; exit 0; }
trap 'git -C "$here" worktree remove --force "$wt/w" 2>/dev/null; rm -rf "$wt"' EXIT

shared="$(run_pair "$here" "$here")"
split="$(run_pair "$here" "$wt/w")"

# The baseline must FAIL, or the isolated arm proves nothing.
if [ "$shared" -eq 0 ]; then
    echo "  FAIL: shared-workspace baseline passed $TRIALS/$TRIALS — control cannot detect the race"; fail=1
else
    echo "  ok   shared workspace collides ($shared/$TRIALS failed)"
fi
if [ "$split" -ne 0 ]; then
    echo "  FAIL: separate worktrees still collided ($split/$TRIALS)"; fail=1
else
    echo "  ok   separate worktrees do not collide (0/$TRIALS)"
fi

[ "$fail" -eq 0 ] && echo "Parallel-lane workspace isolation holds."
exit "$fail"
