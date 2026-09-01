#!/usr/bin/env python3
"""Tests for src/check-pending-questions.py — `Status: open` must count as waiting.

`get_waiting_questions()` counts a `## ` section when its `**Status:**` starts
with `unanswered` or `waiting`, and skips everything else as
"explicitly resolved/done/answered". That silently drops `**Status:** open` —
the most natural word to reach for — so a question filed that way is on disk,
readable, and never surfaced by the notifier.

Covers: `open` (plus case and trailing-prose variants) counts; the existing
`unanswered`/`waiting`/no-status behaviours are unchanged; and — the control —
genuinely resolved statuses and the `# Resolved` region are still NOT counted,
so the fix cannot pass by counting everything.

Run: python3 tests/check-pending-questions-open-status.test.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_pending_questions", REPO / "src" / "check-pending-questions.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_passed = 0
_failed = 0


def ok(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def titles_for(md: str):
    """Run get_waiting_questions() against `md` and return the counted titles."""
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "pending-questions.md"
        f.write_text(md)
        original = _mod.PQ_FILE
        _mod.PQ_FILE = f
        try:
            return {q["title"] for q in _mod.get_waiting_questions()}
        finally:
            _mod.PQ_FILE = original


# T1: `open` and its natural variants count as waiting (the bug).
DOC_OPEN = """# Pending

## Q-open
**Status:** open

Decide whether to merge.

## Q-open-upper
**Status:** OPEN

Decide whether to merge.

## Q-open-prose
**Status:** open — asked in the room, no reply yet

Decide whether to merge.
"""
got = titles_for(DOC_OPEN)
ok("`Status: open` counts", "Q-open" in got)
ok("`Status: OPEN` counts (case-insensitive)", "Q-open-upper" in got)
ok("`Status: open — <prose>` counts", "Q-open-prose" in got)

# T2: no regression — the statuses that already worked still work.
DOC_EXISTING = """# Pending

## Q-unanswered
**Status:** unanswered

body

## Q-waiting
**Status:** Waiting

body

## Q-nostatus

body with no Status field at all
"""
got = titles_for(DOC_EXISTING)
ok("`unanswered` still counts", "Q-unanswered" in got)
ok("`Waiting` still counts", "Q-waiting" in got)
ok("no Status field still counts", "Q-nostatus" in got)

# T3: CONTROL — the discriminator must still be able to say NO. A fix that
# counted every status would pass T1/T2 and be wrong.
DOC_RESOLVED = """# Pending

## Q-resolved
**Status:** resolved

body

## Q-done
**Status:** done

body

## Q-answered
**Status:** answered 2026-07-01

body
"""
got = titles_for(DOC_RESOLVED)
ok("CONTROL: `resolved` still NOT counted", "Q-resolved" not in got)
ok("CONTROL: `done` still NOT counted", "Q-done" not in got)
ok("CONTROL: `answered` still NOT counted", "Q-answered" not in got)

# T4: CONTROL — the `# Resolved` divider still cuts the file. An `open`
# question below it is an audit-trail entry, not a live one.
DOC_DIVIDER = """# Pending

## Q-live
**Status:** open

body

# Resolved

## Q-archived
**Status:** open

body
"""
got = titles_for(DOC_DIVIDER)
ok("`open` above the divider counts", "Q-live" in got)
ok("CONTROL: `open` below `# Resolved` still NOT counted", "Q-archived" not in got)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(0 if _failed == 0 else 1)
