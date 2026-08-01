#!/usr/bin/env python3
"""Tests for scripts/review-preflight.py.

The failure this guards is silent: `CLAUDE.md` and `REVIEW.md` both instruct
reviewers to run a preflight that prints the criteria, and for months no such
file existed — the instruction read as a completed step while surfacing nothing.
So the load-bearing case here is not "it prints lessons" but "it refuses to
succeed quietly when it has no criteria to show".
"""
from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "review-preflight.py"
SPEC = importlib.util.spec_from_file_location("review_preflight", SCRIPT)
assert SPEC and SPEC.loader
pf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pf)

GUIDE = """# Review guide

intro prose

## Lessons (criteria for reviewers)

1. **First lesson.** body
2. **Second lesson.** body

## Checks (machine-readable)

```yaml
checks:
  hardcoded_paths:
    flag:
      - "/Users/"
      - "/home/"
    allow:
      - "tests/fixtures"
```
"""


class ReviewPreflightTest(unittest.TestCase):
    def _guide(self, root: Path, text: str = GUIDE) -> Path:
        p = root / "REVIEW.md"
        p.write_text(text)
        return p

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pf.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_missing_guide_exits_nonzero(self):
        """The whole point: never succeed with nothing to show."""
        with tempfile.TemporaryDirectory() as td:
            code, out, err = self._run(["--guide", str(Path(td) / "nope.md")])
        self.assertEqual(code, 1)
        self.assertIn("guide not found", err)
        self.assertEqual(out, "")

    def test_prints_the_lessons_section(self):
        with tempfile.TemporaryDirectory() as td:
            g = self._guide(Path(td))
            code, out, _ = self._run(["--guide", str(g)])
        self.assertEqual(code, 0)
        self.assertIn("## Lessons (criteria for reviewers)", out)
        self.assertIn("First lesson", out)
        self.assertIn("Second lesson", out)
        self.assertNotIn("intro prose", out)          # only the section, not the file
        self.assertNotIn("hardcoded_paths", out)      # checks block is summarized, not dumped

    def test_guide_without_lessons_warns_rather_than_printing_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            g = self._guide(Path(td), "# Guide\n\n## Checks\n\nnothing here\n")
            code, out, _ = self._run(["--guide", str(g)])
        self.assertEqual(code, 0)
        self.assertIn("WARNING", out)
        self.assertIn("criteria could not be read", out)

    def test_counts_flag_and_allow_patterns(self):
        with tempfile.TemporaryDirectory() as td:
            g = self._guide(Path(td))
            _, out, _ = self._run(["--guide", str(g)])
        self.assertIn("2 flag pattern(s), 1 allow pattern(s)", out)

    def test_pr_number_appears_in_the_header(self):
        with tempfile.TemporaryDirectory() as td:
            g = self._guide(Path(td))
            _, out, _ = self._run(["2495", "--guide", str(g)])
        self.assertIn("Reviewing PR #2495", out)

    def test_guide_resolution_matches_review_checks_order(self):
        """--guide wins; otherwise <repo>/REVIEW.md — same as review-checks.sh."""
        with tempfile.TemporaryDirectory() as td:
            explicit = Path(td) / "elsewhere.md"
            self.assertEqual(pf.resolve_guide(str(explicit)), explicit)
            self.assertEqual(pf.resolve_guide(None, Path(td)), Path(td) / "REVIEW.md")

    def test_runs_against_the_real_repo_guide(self):
        """The shipped REVIEW.md must actually satisfy the parser."""
        code, out, _ = self._run([])
        self.assertEqual(code, 0)
        self.assertIn("Lessons", out)
        self.assertNotIn("WARNING", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
