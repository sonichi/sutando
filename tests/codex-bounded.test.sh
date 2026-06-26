#!/usr/bin/env bash
# Tests for skills/claude-codex/scripts/codex-bounded.sh — the bounded delegation runner.
#   bash tests/codex-bounded.test.sh
set -u
RUNNER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/skills/claude-codex/scripts/codex-bounded.sh"
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

# 4. the whole TREE is killed — a child sleep must not survive the parent kill.
#    Tag the leaf via `exec -a $TAG` so the stray-check matches ONLY this test run's
#    process (scoped by $$) — a bare `pgrep -f "sleep 30"` would false-match any
#    unrelated `sleep 30` on the box.
MARK="/tmp/codex-bounded-test-child.$$"; TAG="codexbnd_tree_$$"; rm -f "$MARK"
bash "$RUNNER" 2 -- bash -c "( exec -a $TAG sleep 30 ) & wait; touch $MARK" >/dev/null 2>&1
sleep 4   # enough that a survivor would still be running
# MARK present ⟹ the parent ran to completion (tree NOT killed); absent ⟹ killed in time
[ -f "$MARK" ] && surv=SURVIVED || surv=clean
check "child process tree killed (no survivor)" clean "$surv"
# assert the uniquely-tagged leaf isn't lingering (no false-match on unrelated sleeps)
pgrep -f "$TAG" >/dev/null 2>&1 && stray=STRAY || stray=none
check "no stray tagged sleep left running" none "$stray"
pkill -f "$TAG" 2>/dev/null   # belt-and-suspenders: reap a survivor so it can't leak
rm -f "$MARK"

# --- stall watchdog (--stall) ---

# 5. a SILENT command (no output) is killed as STALLED → exit 125, promptly
t0=$(date +%s)
bash "$RUNNER" --stall 2 --max 60 -- bash -c 'sleep 30' >/dev/null 2>&1; rc=$?
t1=$(date +%s)
check "silent cmd killed as stalled → exit 125" 125 "$rc"
[ $(( t1 - t0 )) -lt 12 ] && el=ok || el=slow
check "stalled cmd returned promptly (<12s)" ok "$el"

# 6. a command that keeps emitting output is NOT killed (stays under stall window)
#    prints every 1s for ~6s with a 3s stall window → survives → forwards exit 0
bash "$RUNNER" --stall 3 --max 60 -- bash -c 'for i in 1 2 3 4 5 6; do echo tick; sleep 1; done' >/dev/null 2>&1
check "progressing cmd not killed → exit 0" 0 "$?"

# 7. --max backstop fires even when output never stalls → exit 124
#    continuous output (no stall) but exceeds the 3s absolute cap
t0=$(date +%s)
bash "$RUNNER" --stall 30 --max 3 -- bash -c 'while true; do echo tick; sleep 0.5; done' >/dev/null 2>&1; rc=$?
t1=$(date +%s)
check "continuous cmd hits --max cap → exit 124" 124 "$rc"
[ $(( t1 - t0 )) -lt 10 ] && el=ok || el=slow
check "--max cap returned promptly (<10s)" ok "$el"

# 8. back-compat: bare positional integer still means a pure --max deadline (exit 124)
bash "$RUNNER" 2 -- bash -c 'sleep 30' >/dev/null 2>&1
check "positional deadline back-compat → exit 124" 124 "$?"

# --- fixes from the real codex review of this script (2026-06-23) ---

# 9. --max 0 DISABLES the cap (stall-only): a long-but-progressing cmd is NOT killed
#    prints every 1s for ~5s with --stall 30 --max 0 → must complete (exit 0), NOT insta-kill
t0=$(date +%s)
bash "$RUNNER" --stall 30 --max 0 -- bash -c 'for i in 1 2 3 4 5; do echo tick; sleep 1; done' >/dev/null 2>&1; rc=$?
t1=$(date +%s)
check "--max 0 disables cap → exit 0 (not insta-kill)" 0 "$rc"
[ $(( t1 - t0 )) -ge 4 ] && ran=ok || ran=instakill
check "--max 0 did NOT insta-kill (ran ≥4s)" ok "$ran"

# 10. external signal (verdict empty, rc>128) forwards the real exit code, not a false 124
#     command self-kills with SIGTERM → exit 143; runner must forward 143, not relabel 124
bash "$RUNNER" --stall 30 --max 30 -- bash -c 'kill -TERM $$' >/dev/null 2>&1
check "external signal forwarded (143), not false 124" 143 "$?"

[ "$fail" -eq 0 ] && echo PASS || echo FAILED
exit $fail
