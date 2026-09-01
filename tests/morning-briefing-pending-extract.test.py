#!/usr/bin/env python3
"""Tests for morning-briefing.py get_pending_questions() filtering.

Before this fix, every `## ` section title in pending-questions.md was
treated as a pending question — including organizer/section-shell headers
(`## FRESH — …`, `## ACTIVE — …`, `## SURFACED — …`) and already-resolved
items. That made the briefing's "Top item" a section label and inflated the
count.

After the fix, section-shell headers and inline-[RESOLVED] titles are skipped,
so only real open questions are returned (order preserved).
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "morning-briefing.py"


def _load():
    # src/ is on the path so the module's `from util_paths import …` resolves.
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location("morning_briefing", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestGetPendingQuestions(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _run(self, text: str):
        """Write `text` to a temp file and run get_pending_questions against it."""
        with tempfile.TemporaryDirectory() as d:
            pq = Path(d) / "pending-questions.md"
            pq.write_text(text)
            with patch.object(self.mod, "personal_path", return_value=pq):
                return self.mod.get_pending_questions()

    def test_skips_organizer_section_headers(self):
        text = (
            "# Pending questions\n"
            "## FRESH — 2026-07-05 [name]\n"
            "## ACTIVE — 2026-07-04 session\n"
            "## SURFACED — 2026-07-10 [name]\n"
            "## 1. Build the widget? (yes/no)\n"
        )
        qs = self._run(text)
        self.assertEqual(qs, ["1. Build the widget? (yes/no)"])

    def test_skips_inline_resolved(self):
        text = (
            "## 2. [RESOLVED 2026-07-03] shipped already\n"
            "## 3. Force, hand-merge, or leave?\n"
        )
        qs = self._run(text)
        self.assertEqual(qs, ["3. Force, hand-merge, or leave?"])

    def test_preserves_order_of_real_questions(self):
        text = (
            "## FRESH — 2026-07-05 [name]\n"
            "## 1. First question?\n"
            "## 2. Second question?\n"
        )
        qs = self._run(text)
        self.assertEqual(qs, ["1. First question?", "2. Second question?"])

    def test_strips_leading_date_prefix(self):
        text = "## [2026-05-27] Real dated question?\n"
        qs = self._run(text)
        self.assertEqual(qs, ["Real dated question?"])

    def test_resolved_divider_still_cuts(self):
        text = (
            "## 1. Open question?\n"
            "# Resolved\n"
            "## 9. Old answered thing\n"
        )
        qs = self._run(text)
        self.assertEqual(qs, ["1. Open question?"])

    def test_skips_empty_title_header(self):
        # A header that is only a date prefix strips to an empty title and
        # must be skipped (not emitted as a blank question).
        text = (
            "## [2026-07-10]\n"
            "## 1. Real question?\n"
        )
        qs = self._run(text)
        self.assertEqual(qs, ["1. Real question?"])

    def test_missing_file_returns_empty(self):
        with patch.object(
            self.mod, "personal_path", return_value=Path("/nonexistent/pq.md")
        ):
            self.assertEqual(self.mod.get_pending_questions(), [])

    def test_truncates_long_titles_to_60(self):
        long_q = "## " + ("x" * 100) + "\n"
        qs = self._run(long_q)
        self.assertEqual(len(qs), 1)
        self.assertEqual(len(qs[0]), 60)


class SpokenQuestionLineMakesNoRankingClaim(unittest.TestCase):
    """The one question the briefing speaks is index 0 of an UNRANKED list.

    `get_waiting_questions()` yields file order. Calling that "Top item" states a
    priority the code never computed, and only this one line is ever spoken — so a
    high-urgency question below index 0 is both unspoken and implicitly outranked.
    """

    def test_the_line_does_not_call_index_zero_the_top_item(self) -> None:
        mod = _load()
        line = mod.synthesize(None, [], [], [], ["first filed", "urgent but later"], None)
        self.assertIn("2 pending questions", line)
        self.assertIn("first filed", line)
        self.assertNotIn("Top item", line)

    def test_a_single_question_still_reads_naturally(self) -> None:
        mod = _load()
        line = mod.synthesize(None, [], [], [], ["only one"], None)
        self.assertIn("One pending question waiting: only one", line)
        self.assertNotIn("Top item", line)


if __name__ == "__main__":
    unittest.main()
