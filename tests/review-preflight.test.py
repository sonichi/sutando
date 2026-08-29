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
import types
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


class PriorArtTest(unittest.TestCase):
    """The reviewer-facing half: what is ALREADY on the thread.

    Grounded 2026-08-24 — a reviewer checked a PR's *reviews*, found none
    blocking, and filed a REQUEST_CHANGES duplicating a finding a peer had
    posted six minutes earlier as a *comment*. Reviews and comments are
    different endpoints; consulting one and concluding about both is the bug.
    """

    @staticmethod
    def _runner(reviews="[]", comments="[]", rc=0, boom=None):
        class R:
            def __init__(self, out):
                self.returncode = rc
                self.stdout = out

        def run(argv):
            if boom:
                raise boom
            return R(reviews if "/reviews" in argv[2] else comments)

        return run

    def test_a_comment_is_surfaced_even_though_it_is_not_a_review(self):
        run = self._runner(comments='[{"created_at":"2026-08-24T11:36:36Z",'
                                    '"user":{"login":"sonichi"},"body":"the finding"}]')
        seen = pf.prior_art("3327", runner=run)
        self.assertEqual(len(seen), 1)
        self.assertIn("sonichi", seen[0])
        self.assertIn("(comment)", seen[0])

    def test_a_COMMENTED_review_with_a_body_IS_surfaced(self):
        """Regression: filtering on state deleted the record, not a duplicate.

        Measured on #3356 — its ONLY review was a 2805-byte COMMENTED blocking
        finding, and that body is absent from issues/comments, so the old
        `state == "COMMENTED": continue` hid the most substantial prior art on
        the thread. On a repo where agents share a login, a COMMENTED review is
        the only review shape an agent can leave."""
        run = self._runner(reviews='[{"submitted_at":"t","user":{"login":"a"},'
                                   '"state":"COMMENTED","body":"a real finding"}]')
        seen = pf.prior_art("1", runner=run)
        self.assertEqual(len(seen), 1)
        self.assertIn("COMMENTED", seen[0])

    def test_an_empty_bodied_review_is_skipped(self):
        """The real rule is skip-on-EMPTY: an approval with no prose says nothing
        a reviewer needs to read before writing."""
        run = self._runner(reviews='[{"submitted_at":"t","user":{"login":"a"},'
                                   '"state":"APPROVED","body":"   "}]')
        self.assertEqual(pf.prior_art("1", runner=run), [])

    def test_unknown_is_not_empty(self):
        """The load-bearing case: a failed check must not render as a clean one."""
        for label, run in (("gh missing", self._runner(boom=OSError("no gh"))),
                           ("gh failed", self._runner(rc=1)),
                           ("bad json", self._runner(reviews="not json"))):
            with self.subTest(label):
                self.assertIsNone(pf.prior_art("1", runner=run), label)
        unknown = "\n".join(pf.prior_art_block("1", None))
        empty = "\n".join(pf.prior_art_block("1", []))
        self.assertIn("COULD NOT CHECK", unknown)
        self.assertNotIn("COULD NOT CHECK", empty)
        self.assertNotEqual(unknown, empty)

    def test_the_block_says_why_reviews_alone_are_not_enough(self):
        body = "\n".join(pf.prior_art_block("1", ["t  sonichi (comment)"]))
        self.assertIn("COMMENTED", body)
        self.assertIn("sonichi", body)

    def test_a_truncated_list_says_it_is_truncated(self):
        """A bare count above a shorter list reads as a wrong count, not a cut list."""
        many = [f"t{i}  sonichi (comment)" for i in range(63)]
        body = pf.prior_art_block("1", many)
        rows = [ln for ln in body if ln.startswith("  t")]
        self.assertEqual(len(rows), pf.PRIOR_ART_SHOWN)
        self.assertIn(f"showing last {pf.PRIOR_ART_SHOWN} of 63", body[0])
        self.assertNotIn("(63)", body[0])

    def test_an_untruncated_list_does_not_claim_truncation(self):
        few = [f"t{i}  sonichi (comment)" for i in range(pf.PRIOR_ART_SHOWN)]
        head = pf.prior_art_block("1", few)[0]
        self.assertIn(f"({pf.PRIOR_ART_SHOWN})", head)
        self.assertNotIn("showing last", head)


class RepoResolution(unittest.TestCase):
    """`{owner}/{repo}` is gh's REMOTE inference; an app-pinned install has no
    `.git`, so it resolved to nothing and prior-art degraded on every run."""

    def test_explicit_repo_wins(self):
        self.assertEqual(pf.resolve_repo("a/b", env={"SUTANDO_REVIEW_REPO": "c/d"}), "a/b")

    def test_env_is_used_when_no_flag(self):
        self.assertEqual(pf.resolve_repo(None, env={"SUTANDO_REVIEW_REPO": "c/d"}), "c/d")

    def test_remote_inference_is_the_LAST_resort_not_the_only_one(self):
        self.assertEqual(pf.resolve_repo(None, env={}), "{owner}/{repo}")

    def test_the_resolved_repo_reaches_the_gh_call(self):
        seen = []

        def run(argv):
            seen.append(argv)
            return types.SimpleNamespace(returncode=0, stdout="[]")

        pf.prior_art("1", runner=run, repo="a/b")
        self.assertTrue(seen, "no gh call was made")
        self.assertTrue(any("repos/a/b/" in x for x in seen[0]), seen[0])
        self.assertFalse(any("{owner}/{repo}" in x for x in seen[0]), seen[0])

    def test_COULD_NOT_CHECK_is_still_reachable(self):
        """The tell that this fix works is not that the check passes — it is
        that the caveat still fires. A resolution fix that makes it unreachable
        has replaced one silent failure with another."""

        def failing(argv):
            return types.SimpleNamespace(returncode=1, stdout="")

        self.assertIsNone(pf.prior_art("1", runner=failing, repo="a/b"))
        self.assertIn("COULD NOT CHECK", "\n".join(pf.prior_art_block("1", None)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
