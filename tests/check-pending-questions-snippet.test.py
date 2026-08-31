#!/usr/bin/env python3
"""Tests for body-snippet feature in src/check-pending-questions.py.

Regression coverage for PR #1861: pending-question DMs now include the
first non-empty body line as a `snippet` field so the user sees what
action to take, not just that something is waiting.

Run: python3 tests/check-pending-questions-snippet.test.py
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


def ok(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}" + (f" — {detail}" if detail else ""))


def questions_for(text: str) -> list:
    with tempfile.TemporaryDirectory() as td:
        pq = Path(td) / "pending-questions.md"
        pq.write_text(text)
        _mod.PQ_FILE = pq
        return _mod.get_waiting_questions()


# 1. Section with body produces snippet (first non-empty line)
qs = questions_for(
    "## [2026-06-30] sync-workspace needs vault remote URL\n"
    "To enable cross-machine workspace sync, run --init first.\n\n"
    "**What's needed**: a private git repo URL.\n"
)
ok("section body → snippet present", len(qs) == 1 and bool(qs[0].get("snippet")),
   f"got {qs}")
ok("section body → snippet is first line",
   len(qs) == 1 and qs[0].get("snippet", "").startswith("To enable cross-machine"),
   f"got snippet: {qs[0].get('snippet') if qs else 'N/A'}")

# 2. Strikethrough lines are skipped; first non-strikethough line wins
qs = questions_for(
    "## [done-ish] something\n"
    "~~RESOLVED — no longer relevant~~\n"
    "Actual action: run foo to fix.\n"
)
ok("strikethrough skipped → first real line is snippet",
   len(qs) == 1 and "Actual action" in qs[0].get("snippet", ""),
   f"got snippet: {qs[0].get('snippet') if qs else 'N/A'}")

# 2b. Regression for reviewer finding (liususan091219, 2026-07-12): a section
# whose **Status:** line comes before the narrative text must not DM the
# status marker itself as the "action hint" — that tells the user nothing.
qs = questions_for(
    "## needs a decision\n"
    "**Status:** unanswered\n"
    "Pick option A or B before Thursday.\n"
)
ok("status line skipped → first real line is snippet",
   len(qs) == 1 and qs[0].get("snippet", "") == "Pick option A or B before Thursday.",
   f"got snippet: {qs[0].get('snippet') if qs else 'N/A'}")
ok("status line never appears as the snippet",
   len(qs) == 1 and "Status" not in qs[0].get("snippet", ""),
   f"got snippet: {qs[0].get('snippet') if qs else 'N/A'}")

# 3. Section with no body → snippet is empty string, not missing
qs = questions_for("## [no-body] empty section\n\n")
ok("empty body → snippet is empty string",
   len(qs) == 1 and qs[0].get("snippet", "MISSING") == "",
   f"got snippet: {qs[0].get('snippet', 'MISSING') if qs else 'N/A'}")

# 4. Bullet-format entries have no snippet (one-liners, body = "")
qs = questions_for("  - **[bullet item, 2026-06-30]** action text here\n")
# #1861 asserted bullets carry no snippet because at the time they were
# one-liners whose label WAS the content. The format outgrew that premise.
ok("bullet entry → snippet is the ask after the label",
   len(qs) == 1 and qs[0].get("snippet") == "action text here",
   f"got {qs[0] if qs else 'N/A'}")

qs_bare = questions_for("  - **[bare label, 2026-06-30]**\n")
ok("label-only bullet → still no snippet",
   len(qs_bare) == 1 and not qs_bare[0].get("snippet"),
   f"got {qs_bare[0] if qs_bare else 'N/A'}")

# 5. notify_discord_dm renders arrow-snippet under title when present
qs_with_snippet = [{"title": "pending question", "snippet": "Run bash --init to fix."}]
qs_no_snippet = [{"title": "bullet question", "snippet": ""}]
with tempfile.TemporaryDirectory() as td:
    _mod.RESULTS_DIR = Path(td)
    _mod.notify_discord_dm(qs_with_snippet)
    result_files = list(Path(td).glob("proactive-pending-q-*.txt"))
    content = result_files[0].read_text() if result_files else ""
ok("DM with snippet contains arrow line",
   "↳ Run bash --init to fix." in content,
   f"DM content:\n{content}")
ok("DM with snippet still has title",
   "• pending question" in content,
   f"DM content:\n{content}")

with tempfile.TemporaryDirectory() as td:
    _mod.RESULTS_DIR = Path(td)
    _mod.notify_discord_dm(qs_no_snippet)
    result_files = list(Path(td).glob("proactive-pending-q-*.txt"))
    content_no = result_files[0].read_text() if result_files else ""
ok("DM without snippet has no arrow line",
   "↳" not in content_no,
   f"DM content:\n{content_no}")

total = _passed + _failed
print(f"check-pending-questions-snippet: {_passed}/{total} passed"
      + ("" if _failed == 0 else f" — {_failed} FAILED"))
sys.exit(0 if _failed == 0 else 1)
