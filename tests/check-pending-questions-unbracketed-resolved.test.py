#!/usr/bin/env python3
"""Tests for src/check-pending-questions.py — unbracketed "RESOLVED <ts> -- ..."
titles must count as resolved, not waiting.

`_INLINE_RESOLVED` only recognized a bracketed resolution marker
("## [RESOLVED 2026-07-03] shipped"). The proactive-loop's own free-form prose
convention writes resolutions unbracketed instead: "## RESOLVED 2026-09-25T23:42Z
-- sync-workspace pushed ... (was: ...)". Neither `_INLINE_RESOLVED`'s bracket
grammar nor `_ORG_HEADING`'s keyword-then-immediate-separator rule catches this
(a timestamp sits between the keyword and the dash), so every section written
this way fell through to "no Status field: free-form prose is unanswered by
convention" and counted as still waiting. Measured on the live per-host file:
46 of 297 `## ` sections used exactly this shape, all 46 genuinely resolved.

Covers: unbracketed RESOLVED/DONE/ANSWERED immediately followed by a
YYYY-MM-DD-shaped timestamp is NOT counted as waiting; the existing bracketed
form is unchanged; and — the control — a title that merely starts with one of
these words followed by ordinary prose (not a timestamp) still counts as a
live question, so the fix cannot pass by matching the keyword alone.

Run: python3 tests/check-pending-questions-unbracketed-resolved.test.py
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
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "pending-questions.md"
        f.write_text(md)
        original = _mod.PQ_FILE
        _mod.PQ_FILE = f
        try:
            return {q["title"] for q in _mod.get_waiting_questions()}
        finally:
            _mod.PQ_FILE = original


# T1: unbracketed RESOLVED/DONE/ANSWERED + immediate timestamp is NOT counted.
DOC_UNBRACKETED = """# Pending

## RESOLVED 2026-09-25T23:42Z -- sync-workspace pushed the 292-file deletion cleanly (was: refused push)

body

## RESOLVED 2026-09-20T~00:2xZ -- Chi confirmed final wording directly in chat

body

## DONE 2026-08-18 -- AV Luminary video SHIPPED

body

## ANSWERED 2026-09-18T19:15Z -- Chi supplied the ORCID

body

## 3. RESOLVED 2026-09-13T17:24Z -- #4230 MERGED as 64841206

body
"""
got = titles_for(DOC_UNBRACKETED)
for t in got:
    ok(f"unbracketed timestamped marker NOT counted: {t[:40]!r}", False)
ok("all 5 unbracketed+timestamp titles excluded", len(got) == 0)

# T2: no regression — the existing bracketed form still works.
DOC_BRACKETED = """# Pending

## [RESOLVED 2026-07-03] shipped already

body

## 2. [RESOLVED 2026-07-03] shipped already too

body
"""
got = titles_for(DOC_BRACKETED)
ok("bracketed form still excluded", len(got) == 0)

# T3: CONTROL — a title that starts with the keyword but is NOT followed by a
# timestamp is a live sentence, not a resolution stamp, and must still count.
DOC_LIVE = """# Pending

## Resolved the conflict, but still need a second look

body

## Answered by voicemail, no callback yet -- still open

body

## Done reviewing #4230, one more pass needed

body
"""
got = titles_for(DOC_LIVE)
ok("CONTROL: keyword+prose (no timestamp) still counts: Resolved…",
   "Resolved the conflict, but still need a second look" in got)
ok("CONTROL: keyword+prose (no timestamp) still counts: Answered…",
   "Answered by voicemail, no callback yet -- still open" in got)
ok("CONTROL: keyword+prose (no timestamp) still counts: Done…",
   "Done reviewing #4230, one more pass needed" in got)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(0 if _failed == 0 else 1)
