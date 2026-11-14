#!/usr/bin/env python3
"""A friction report must not be a second copy of the pending-questions list: past a
threshold the section collapses to a count plus a sample, below it is unchanged.
Items come from the one reader (mocked here)."""
from pathlib import Path
import importlib.util
import tempfile
import time
import unittest
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src" / "friction-detector.py"


def _load(workspace: Path):
    spec = importlib.util.spec_from_file_location("fd_collapse", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.WORKSPACE = workspace
    return m


def _sections(n, dated_from=None):
    """n open items; if dated_from is set, each carries an asked_at that many days back."""
    out = []
    for i in range(n):
        asked = time.time() - (dated_from + i) * 86400 if dated_from is not None else None
        out.append({"id": f"q{i}", "ask_id": f"ask-{i}", "title": f"Question {i}", "snippet": "body text",
                    "body": "body text", "asked_at": asked, "priority": "medium", "in_room": True})
    return out


class Collapse(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _run(self, items):
        m = _load(self.ws)
        g = {"waiting": items, "done": 0, "unavailable": False, "reason": None, "notes": [], "link": None}
        with mock.patch.object(m.pending_questions_reader, "gather", return_value=g):
            return m.check_pending_questions()

    def test_short_lists_are_still_enumerated_in_full(self):
        """The change must not alter behaviour below the threshold."""
        m = _load(self.ws)
        out = self._run(_sections(m._PQ_ENUMERATE_MAX))
        self.assertEqual(len(out), m._PQ_ENUMERATE_MAX)
        self.assertTrue(all(l.startswith("Pending question unanswered") for l in out))

    def test_a_long_list_collapses_instead_of_dumping(self):
        m = _load(self.ws)
        n = 52
        out = self._run(_sections(n))
        self.assertLessEqual(len(out), m._PQ_OLDEST_SHOWN + 1,
                             f"52 questions still produced {len(out)} lines")
        self.assertIn(f"{n} pending questions", out[0])

    def test_the_collapsed_count_is_the_REAL_count(self):
        """A summary that under-counts is worse than the dump it replaced."""
        out = self._run(_sections(37))
        self.assertIn("37 pending questions", out[0])

    def test_it_names_the_tool_that_owns_the_full_list(self):
        out = self._run(_sections(20))
        self.assertIn("check-pending-questions", out[0])

    def test_undated_sections_are_NOT_labelled_oldest(self):
        """Sorting is a no-op with no dates; calling the first three 'oldest'
        is a label the data cannot support."""
        out = self._run(_sections(20))
        self.assertNotIn("oldest", out[0],
                         f"claimed an ordering it does not have: {out[0]!r}")
        self.assertIn("including", out[0])

    def test_dated_sections_ARE_labelled_oldest_and_sorted(self):
        out = self._run(_sections(20, dated_from=10))
        self.assertIn("oldest", out[0])
        # Question 19 is the oldest (dated_from + 19 days).
        self.assertIn("Question 19", out[1])
        self.assertIn("Question 18", out[2])

    def test_a_mixed_file_ranks_the_dated_ones(self):
        """Undated entries must not displace a genuinely old dated one."""
        body = _sections(20) + [{"id": "anc", "ask_id": "ask-anc", "title": "Ancient dated question",
                                 "snippet": "body", "body": "body", "asked_at": time.time() - 400 * 86400,
                                 "priority": "medium", "in_room": True}]
        out = self._run(body)
        self.assertIn("oldest", out[0])
        self.assertIn("Ancient dated question", out[1])

    def test_a_fresh_question_is_not_stale_and_an_empty_list_returns_nothing(self):
        body = _sections(8) + [{"id": "fresh", "ask_id": "ask-fresh", "title": "Fresh", "snippet": "body",
                                "body": "body", "asked_at": time.time() - 3600, "priority": "medium",
                                "in_room": True}]
        out = self._run(body)
        self.assertIn("8 pending questions", out[0])
        self.assertEqual(self._run([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
