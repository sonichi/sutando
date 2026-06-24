#!/usr/bin/env python3
"""Tests for src/check-pending-questions.py — free-form bullet format support.

Regression coverage for the silent-no-notify bug: the parser walked only
`## ` sections, but the real pending-questions.md (written by the proactive
loop and skills) uses `- **[label, ts]** ...` bullets with zero `## ` headings,
so get_waiting_questions() returned [] and every notification was suppressed.

Complements tests/check-pending-questions.test.py (which covers the `## `
section formats); kept in a separate file to avoid merge conflicts.

Run: python3 tests/check-pending-questions-bullet.test.py
Exit: 0 on pass, 1 on fail.
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


def check(name, text, expected_titles):
    global _passed, _failed
    with tempfile.TemporaryDirectory() as td:
        pq = Path(td) / "pending-questions.md"
        pq.write_text(text)
        _mod.PQ_FILE = pq
        got = [q["title"] for q in _mod.get_waiting_questions()]
    if got == expected_titles:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}\n    expected: {expected_titles}\n    got:      {got}")


# a) bullet items are counted (the production case that was silently broken)
check(
    "bullet items counted",
    "# Pending Questions\n\n"
    "  - **[finding/reliability, 2026-06-11T01:56Z]** Voice timers dropped.\n"
    "  - **[set_timer spec, 2026-06-11T01:56Z]** Build it?\n",
    ["finding/reliability, 2026-06-11T01:56Z", "set_timer spec, 2026-06-11T01:56Z"],
)

# b) bullets below a `# Resolved` divider are excluded
check(
    "resolved-region bullets excluded",
    "  - **[active item, ts]** still open\n\n"
    "# Resolved\n\n  - **[old item, ts]** done\n",
    ["active item, ts"],
)

# c) `## ` sections still parse (no regression to the section format)
check(
    "## sections still parsed",
    "## Q1 — First\n\n**Status:** unanswered\n\nbody\n\n## Q2 — Second\n\nno status\n",
    ["Q1 — First", "Q2 — Second"],
)

# d) mixed section + bullet, de-duplicated by title
check(
    "mixed format de-duplicated",
    "## SectionTitle\n\nbody\n\n"
    "  - **[SectionTitle]** dup, must not double-count\n"
    "  - **[unique bullet, ts]** keep\n",
    ["SectionTitle", "unique bullet, ts"],
)

# e) empty file -> no questions
check("empty file -> none", "", [])

total = _passed + _failed
print(f"check-pending-questions-bullet: {_passed}/{total} passed"
      + ("" if _failed == 0 else f" — {_failed} FAILED"))
sys.exit(0 if _failed == 0 else 1)
