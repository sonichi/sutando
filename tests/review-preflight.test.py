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


class ReviewPreflightErrorPathTest(unittest.TestCase):
    """The two failure branches CI measured as uncovered (91.8% diff coverage).

    Both are the paths that run when the environment is degraded, which is
    exactly when a reviewer needs the tool to behave predictably rather than
    traceback.
    """

    def test_repo_root_falls_back_when_git_is_unavailable(self):
        """`git rev-parse` raising must fall back to the script's own location."""
        import unittest.mock as mock

        def boom(*a, **kw):
            raise FileNotFoundError("git not on PATH")

        with mock.patch.object(pf.subprocess, "run", side_effect=boom):
            root = pf.repo_root()
        # The fallback is <script dir>/.. — i.e. the repo containing scripts/.
        self.assertEqual(root, Path(pf.__file__).resolve().parent.parent)
        self.assertTrue((root / "REVIEW.md").is_file(),
                        "fallback must still locate the shipped guide")

    def test_repo_root_falls_back_when_git_returns_empty(self):
        """A git that succeeds but prints nothing must not yield Path('')."""
        import subprocess as _sp
        import unittest.mock as mock
        done = _sp.CompletedProcess(args=[], returncode=0, stdout="   \n", stderr="")
        with mock.patch.object(pf.subprocess, "run", return_value=done):
            root = pf.repo_root()
        self.assertEqual(root, Path(pf.__file__).resolve().parent.parent)

    def test_unreadable_guide_exits_nonzero_without_traceback(self):
        """A guide that exists but cannot be read reports and exits 1."""
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as td:
            g = Path(td) / "REVIEW.md"
            g.write_text(GUIDE)
            with mock.patch.object(pf, "render", side_effect=OSError("EIO")):
                code, out, err = self._run_capture(["--guide", str(g)])
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)
        self.assertIn("EIO", err)
        self.assertEqual(out, "")

    def _run_capture(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pf.main(argv)
        return code, out.getvalue(), err.getvalue()


if __name__ == "__main__":
    unittest.main(verbosity=1)
