#!/usr/bin/env python3
"""A staleness probe must not read a newest-first default window: `gh issue list`
returns 30 rows newest-first, so looking for the OLDEST sees the least stale."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import importlib.util
import json
import subprocess
import types
import unittest

SRC = Path(__file__).resolve().parent.parent / "src" / "friction-detector.py"
_spec = importlib.util.spec_from_file_location("fd", SRC)
fd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fd)


def _issue(n, age_days):
    ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat().replace("+00:00", "Z")
    return {"number": n, "title": f"issue {n} aged {age_days}d", "updatedAt": ts}


class _FakeGh:
    """Records argv and returns a canned issue list."""

    def __init__(self, items, returncode=0):
        self.items = items
        self.returncode = returncode
        self.argv = None

    def __call__(self, argv, **kw):
        self.argv = argv
        return types.SimpleNamespace(
            returncode=self.returncode, stdout=json.dumps(self.items), stderr="")


class GithubProbeBounds(unittest.TestCase):
    def setUp(self):
        self._real = fd.subprocess.run

    def tearDown(self):
        fd.subprocess.run = self._real

    def _run(self, items, returncode=0):
        fake = _FakeGh(items, returncode)
        fd.subprocess.run = fake
        return fd.check_github_issues(), fake

    def test_the_query_passes_an_explicit_limit(self):
        """Pre-fix this argv had no --limit, so gh's default 30 applied."""
        _, fake = self._run([_issue(1, 30)])
        self.assertIn("--limit", fake.argv,
                      "no --limit means gh's default 30-row, newest-first window")
        i = fake.argv.index("--limit")
        self.assertGreaterEqual(int(fake.argv[i + 1]), 100,
                                "a limit at or below the default does not widen anything")

    def test_a_truncated_read_is_reported_as_UNCHECKED_not_as_findings(self):
        """count == the cap we passed means the oldest rows may be missing."""
        items = [_issue(n, 30) for n in range(fd._GH_QUERY_LIMIT)]
        out, _ = self._run(items)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith(fd.UNCHECKED),
                        f"a capped read must not render as a clean finding list: {out[0]!r}")

    def test_it_reports_the_OLDEST_first(self):
        """The pre-fix loop emitted gh's newest-first order verbatim."""
        out, _ = self._run([_issue(1, 8), _issue(2, 400), _issue(3, 50)])
        self.assertIn("#2", out[0], f"oldest (400d) must lead, got {out[0]!r}")
        self.assertIn("#3", out[1])

    def test_fresh_issues_are_not_reported(self):
        out, _ = self._run([_issue(1, 1), _issue(2, 3)])
        self.assertEqual(out, [])

    def test_the_report_is_capped_and_says_how_many_it_dropped(self):
        """109 findings is a nag nobody opens; silence about the rest is worse."""
        out, _ = self._run([_issue(n, 100 + n) for n in range(40)])
        self.assertEqual(len(out), fd._GH_REPORT_CAP + 1,
                         "expected the cap plus one overflow line")
        self.assertIn(f"{40 - fd._GH_REPORT_CAP} more", out[-1],
                      f"the dropped count must be stated, got {out[-1]!r}")

    def test_exactly_at_the_report_cap_adds_no_overflow_line(self):
        out, _ = self._run([_issue(n, 100 + n) for n in range(fd._GH_REPORT_CAP)])
        self.assertEqual(len(out), fd._GH_REPORT_CAP)
        self.assertNotIn("more stale", out[-1])

    def test_a_failed_gh_still_reports_UNCHECKED(self):
        """Pre-existing behaviour that must survive the change."""
        out, _ = self._run([], returncode=1)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith(fd.UNCHECKED))

    def test_it_queries_issues_not_prs(self):
        """Open PRs are pr_flag.py's domain; querying both double-reports."""
        _, fake = self._run([_issue(1, 30)])
        self.assertIn("issue", fake.argv)
        self.assertNotIn("pr", fake.argv)


class DocstringMatchesBehaviour(unittest.TestCase):
    def test_the_docstring_does_not_promise_PRs(self):
        """It said 'issues/PRs' while querying only issues."""
        doc = fd.check_github_issues.__doc__ or ""
        self.assertNotIn("/PRs", doc,
                         "docstring claims PRs the query never fetches")


if __name__ == "__main__":
    unittest.main(verbosity=2)
