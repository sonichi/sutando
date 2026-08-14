#!/usr/bin/env python3
"""A friction report must not be a second copy of pending-questions.md: past a
threshold the section collapses to a count plus a sample, below it is unchanged."""
from datetime import date, timedelta
from pathlib import Path
import importlib.util
import tempfile
import unittest

SRC = Path(__file__).resolve().parent.parent / "src" / "friction-detector.py"


def _load(workspace: Path):
    spec = importlib.util.spec_from_file_location("fd_collapse", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.WORKSPACE = workspace
    return m


def _write(workspace: Path, body: str):
    pq = Path(_load(workspace).personal_path("pending-questions.md", workspace))
    pq.parent.mkdir(parents=True, exist_ok=True)
    pq.write_text(body)
    return pq


def _sections(n, dated_from=None):
    """n open sections; if dated_from is set, each carries an **Asked:** date."""
    out = []
    for i in range(n):
        out.append(f"## Question {i}")
        if dated_from is not None:
            d = date.today() - timedelta(days=dated_from + i)
            out.append(f"**Asked:** {d.isoformat()}")
        out.append("body text")
        out.append("")
    return "\n".join(out)


class Collapse(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _run(self, body):
        _write(self.ws, body)
        return _load(self.ws).check_pending_questions()

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
        body = _sections(20) + "\n" + (
            "## Ancient dated question\n"
            f"**Asked:** {(date.today() - timedelta(days=400)).isoformat()}\n"
            "body\n")
        out = self._run(body)
        self.assertIn("oldest", out[0])
        self.assertIn("Ancient dated question", out[1])

    def test_resolved_sections_are_still_excluded_from_the_count(self):
        body = _sections(8) + "\n## Done one\n**Status:** resolved\nbody\n"
        out = self._run(body)
        self.assertIn("8 pending questions", out[0])

    def test_an_empty_file_still_returns_nothing(self):
        self.assertEqual(self._run("(No pending questions)"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
