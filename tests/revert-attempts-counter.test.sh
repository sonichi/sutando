#!/usr/bin/env bash
# tests/revert-attempts-counter.test.sh — verify attempts-counter removal (#1075)
#
# Confirms that task_bump_attempts.py is gone and watch-tasks-stream.sh
# no longer references it. The /task-orphan-check skill (#1074) replaces this.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0; FAIL=0
pass() { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }

echo "━━━ revert-attempts-counter (#1075) tests ━━━"

# T1 — task_bump_attempts.py is deleted
if [ ! -f "$REPO_ROOT/src/task_bump_attempts.py" ]; then
    pass "T1: src/task_bump_attempts.py is gone"
else
    fail "T1: src/task_bump_attempts.py still exists — should have been deleted"
fi

# T2 — task-bump-attempts test file is deleted
if [ ! -f "$REPO_ROOT/tests/task-bump-attempts.test.py" ]; then
    pass "T2: tests/task-bump-attempts.test.py is gone"
else
    fail "T2: tests/task-bump-attempts.test.py still exists — should have been deleted"
fi

# T3 — watch-tasks-stream.sh does not reference task_bump_attempts
WATCHER="$REPO_ROOT/src/watch-tasks-stream.sh"
if ! grep -q "task_bump_attempts" "$WATCHER" 2>/dev/null; then
    pass "T3: watch-tasks-stream.sh has no reference to task_bump_attempts"
else
    fail "T3: watch-tasks-stream.sh still references task_bump_attempts"
fi

# T4 — watch-tasks-stream.sh does not reference SCRIPT_DIR (no longer needed)
if ! grep -q "SCRIPT_DIR" "$WATCHER" 2>/dev/null; then
    pass "T4: SCRIPT_DIR variable removed from watch-tasks-stream.sh"
else
    fail "T4: SCRIPT_DIR still present — likely a stale reference"
fi

# T5 — watch-tasks-stream.sh does not reference LAST_EMIT dedup vars
if ! grep -qE "LAST_EMIT_BN|LAST_EMIT_SEC" "$WATCHER" 2>/dev/null; then
    pass "T5: LAST_EMIT dedup variables removed from watch-tasks-stream.sh"
else
    fail "T5: LAST_EMIT variables still present — stale cooldown dedup"
fi

# T6 — TASK_FILE: emission present in watcher (sh wrapper or .mjs implementation)
# stando uses a Node wrapper (watch-tasks-stream.mjs) so the emit lives there,
# not in the .sh shim. Accept either location.
MJS="$REPO_ROOT/src/watch-tasks-stream.mjs"
if grep -q 'TASK_FILE:' "$WATCHER" 2>/dev/null || grep -q 'TASK_FILE:' "$MJS" 2>/dev/null; then
    pass "T6: TASK_FILE: emit present (sh or mjs)"
else
    fail "T6: TASK_FILE: emit missing from both watch-tasks-stream.sh and .mjs"
fi

# T7 — watch-tasks-stream.sh syntax is valid bash
if bash -n "$WATCHER" 2>/dev/null; then
    pass "T7: watch-tasks-stream.sh passes bash syntax check"
else
    fail "T7: watch-tasks-stream.sh has syntax errors"
fi

echo ""
echo "━━━ Summary ━━━"
echo "PASS: $PASS"
echo "FAIL: $FAIL"
[ "$FAIL" -eq 0 ] && echo "revert-attempts-counter tests OK." || exit 1
