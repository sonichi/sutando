#!/usr/bin/env bash
# Tests for scripts/codex-bounded.sh — the bounded sandboxed-delegation runner.
#   bash tests/codex-bounded.test.sh
set -u
RUNNER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/codex-bounded.sh"
fail=0
check(){ if [ "$2" = "$3" ]; then printf 'ok   - %s\n' "$1"; else printf 'FAIL - %s (want=%q got=%q)\n' "$1" "$2" "$3"; fail=1; fi; }

# 1. fast command completes and forwards its exit code (0)
bash "$RUNNER" 5 -- bash -c 'exit 0'; check "fast cmd forwards exit 0" 0 "$?"

# 2. forwards a non-zero exit
bash "$RUNNER" 5 -- bash -c 'exit 7'; check "forwards exit 7" 7 "$?"

# 3. an overrunning command is killed on the deadline → exit 124
t0=$(date +%s)
bash "$RUNNER" 2 -- bash -c 'sleep 30'; rc=$?
t1=$(date +%s)
check "overrun killed → exit 124" 124 "$rc"
# and it returned promptly (well under the 30s sleep)
[ $(( t1 - t0 )) -lt 10 ] && el=ok || el=slow
check "overrun returned promptly (<10s)" ok "$el"

# 4. the whole TREE is killed — a child sleep must not survive the parent kill
MARK="/tmp/codex-bounded-test-child.$$"; rm -f "$MARK"
bash "$RUNNER" 2 -- bash -c "( sleep 30 && touch $MARK ) & wait" >/dev/null 2>&1
sleep 4   # past the original child's 30s? no — just enough that a survivor would still be running
# if any descendant survived it'd still be sleeping; assert no lingering child sleep wrote the mark
[ -f "$MARK" ] && surv=SURVIVED || surv=clean
check "child process tree killed (no survivor)" clean "$surv"
# also assert no stray 'sleep 30' from this test lingers
pgrep -f "sleep 30" >/dev/null 2>&1 && stray=STRAY || stray=none
check "no stray sleep left running" none "$stray"
rm -f "$MARK"

[ "$fail" -eq 0 ] && echo PASS || echo FAILED
exit $fail
