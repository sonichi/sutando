#!/usr/bin/env python3
"""Regression guard: /task-done must not register voice-*/proactive-* ids in
the Task list (issue #1786).

Root cause: task-bridge.ts passes voice-* and proactive-* result files to
/task-done, which previously created a new task_history row for each fire.
Per-fire timestamps make these ids unique, so N re-fires = N duplicate rows.

Fix: /task-done silently accepts (200 OK) but does NOT store non-task- ids in
task_history. This test is structural — it verifies the guard exists in
src/agent-api.py without spinning up the HTTP server.

Run: python3 tests/agent-api-task-done-id-guard.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "agent-api.py"

errors = 0


def fail(msg: str, context: str = "") -> None:
    global errors
    print(f"FAIL: {msg}", file=sys.stderr)
    if context:
        print(f"  context: {context[:300]}", file=sys.stderr)
    errors += 1


# --- Structural checks on the /task-done handler ---

src = SRC.read_text()

# 1. Guard exists: `if not tid.startswith("task-"):`
guard_pat = re.compile(r'if\s+not\s+tid\.startswith\(\s*["\']task-["\']\s*\)')
if not guard_pat.search(src):
    fail(
        'missing `if not tid.startswith("task-"):` guard in /task-done handler',
        "Expected guard to reject voice-*/proactive-* ids from task_history",
    )
else:
    print("  ok  guard exists: `if not tid.startswith(\"task-\"):`")

# 2. Guard returns 200 + skips the task_history write — find the pattern
#    where the guard block calls send_json(200, ...) and returns before any
#    task_history assignment.
# Locate the guard match and check what follows it.
guard_match = guard_pat.search(src)
if guard_match:
    after_guard = src[guard_match.end(): guard_match.end() + 200]
    # Must send 200 and return within the guard block.
    if 'send_json(200' not in after_guard or 'return' not in after_guard:
        fail(
            "guard exists but does not send_json(200) + return — "
            "non-task ids may still reach task_history",
            after_guard,
        )
    else:
        print("  ok  guard sends 200 and returns before task_history write")

# 3. task_history assignment appears AFTER the guard — ensure the guard
#    precedes the `task_history[tid]` write lines.
th_pat = re.compile(r'task_history\[tid\]')
th_match = th_pat.search(src[guard_match.end():]) if guard_match else None
if not guard_match:
    pass  # already failed above
elif not th_match:
    fail("no task_history[tid] write found after guard — guard logic unclear")
else:
    print("  ok  task_history write follows the guard (correct ordering)")

# 4. Structural: the guard must be inside the /task-done handler.
#    Find the /task-done path check and locate the guard within it.
handler_pat = re.compile(r'path\s*==\s*["\']\/task-done["\']')
handler_match = handler_pat.search(src)
if not handler_match:
    fail('"/task-done" path handler not found in agent-api.py')
else:
    handler_start = handler_match.start()
    if guard_match and guard_match.start() < handler_start:
        fail("guard appears BEFORE the /task-done handler — it won't apply")
    else:
        print("  ok  guard is inside the /task-done handler")

# Report
if errors:
    print(f"\nFAILED: {errors} check(s) failed", file=sys.stderr)
    sys.exit(1)
else:
    print(f"\nPASSED: /task-done id guard present and correctly ordered")
