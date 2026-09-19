#!/usr/bin/env python3
"""`--assert-head` refuses to let a review be posted against a moved head.

A preflight runs before you compose; the head can move while you write. The
gap is longest for the findings worth the most care, which is the wrong way
round. Peer report: a finding posted 29 seconds after the fix that answered it,
with the verified sha correctly quoted in the comment body — naming the head is
not the same as re-reading it.

Run: python3 tests/review-preflight-assert-head.test.py
"""
import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "_rp", REPO / "skills" / "review-preflight" / "scripts" / "review-preflight.py")
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

HEAD = "f89fec9ef92f45455fd025f35773b0bff9ff6474"


def _run(argv, head):
    """main() with current_head pinned; returns (rc, stdout, stderr)."""
    real = rp.current_head
    rp.current_head = lambda pr, runner=None, repo=None: head
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = rp.main(argv)
    finally:
        rp.current_head = real
    return rc, out.getvalue(), err.getvalue()


class AssertHead(unittest.TestCase):
    def test_unchanged_head_passes(self):
        rc, out, _ = _run(["3774", "--assert-head", HEAD], HEAD)
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)

    def test_a_prefix_of_the_head_is_accepted(self):
        """Short shas are what a person actually copies out of a previous run."""
        rc, out, _ = _run(["3774", "--assert-head", HEAD[:9]], HEAD)
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)

    def test_a_TOO_SHORT_sha_is_refused_not_quietly_accepted(self):
        """The prefix compare is what makes short shas usable, and it is also what
        would let `-​-assert-head f` match five sixteenths of all heads. A guard
        whose weak input silently passes is the failure this guard exists for."""
        for short in ("f", "f89", "f89fec"):
            rc, _, err = _run(["3774", "--assert-head", short], HEAD)
            self.assertEqual(rc, 2, f"{short!r} must be a usage error")
            self.assertIn("too short", err)

    def test_seven_characters_is_accepted(self):
        """The boundary itself, so the cutoff cannot drift without a test failing."""
        rc, out, _ = _run(["3774", "--assert-head", HEAD[:7]], HEAD)
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)

    def test_MOVED_head_fails(self):
        rc, _, err = _run(["3774", "--assert-head", "637d3d37b"], HEAD)
        self.assertEqual(rc, 3)
        self.assertIn("MOVED", err)

    def test_UNREADABLE_head_fails_rather_than_passing(self):
        """The failure that matters: not-read must never read as not-moved."""
        rc, _, err = _run(["3774", "--assert-head", HEAD], None)
        self.assertEqual(rc, 3)
        self.assertIn("could not read", err)

    def test_missing_pr_number_is_a_usage_error_not_a_pass(self):
        rc, _, err = _run(["--assert-head", HEAD], HEAD)
        self.assertEqual(rc, 2)
        self.assertIn("needs the PR number", err)

    def test_without_the_flag_the_guide_still_renders(self):
        """The flag is additive: the normal preflight path must be untouched."""
        rc, out, _ = _run(["3774"], HEAD)
        self.assertEqual(rc, 0)
        self.assertIn("Lessons", out)


class CurrentHead(unittest.TestCase):
    """Drive `current_head` itself. Every test above replaces it, so its own body —
    the gh-failed branch and the empty-sha conversion — was never executed, and the
    unreadable polarity was inherited from `_gh_json` rather than pinned here."""

    class _Proc:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def _runner(self, rc=0, out=""):
        return lambda argv: self._Proc(rc, out)

    def test_reads_the_sha(self):
        body = json.dumps({"head": {"sha": HEAD}})
        self.assertEqual(rp.current_head("1", runner=self._runner(out=body),
                                         repo="o/r"), HEAD)

    def test_gh_failure_is_None_not_an_empty_answer(self):
        self.assertIsNone(rp.current_head("1", runner=self._runner(rc=1), repo="o/r"))

    def test_unparseable_body_is_None(self):
        self.assertIsNone(rp.current_head("1", runner=self._runner(out="{not json"),
                                          repo="o/r"))

    def test_an_EMPTY_sha_is_None_not_the_empty_string(self):
        """`""` would compare falsely against any --assert-head and could reach the
        moved/unchanged branch with nothing in it."""
        for body in ('{"head": {"sha": ""}}', '{"head": {}}', '{}'):
            self.assertIsNone(rp.current_head("1", runner=self._runner(out=body),
                                              repo="o/r"), body)

    def test_a_raising_runner_is_None(self):
        def boom(argv):
            raise OSError("gh not on PATH")
        self.assertIsNone(rp.current_head("1", runner=boom, repo="o/r"))


class ExplicitEmptyAssertHead(unittest.TestCase):
    """`--assert-head ''` must not skip the check the flag exists to perform.

    argparse gives None when the option is absent and "" when it is passed
    empty; a truthiness test collapses those, so the whole head-assertion
    branch was bypassed and main() fell through to ordinary guide rendering.
    """

    def _run(self, argv):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            try:
                rc = rp.main(argv)
            except SystemExit as exc:      # argparse usage errors
                rc = exc.code
        return rc, err.getvalue()

    def test_empty_WITH_a_pr_is_a_usage_error_not_a_pass(self):
        rc, err = self._run(["--assert-head", "", "3781"])
        self.assertEqual(rc, 2, err)
        self.assertIn("too short", err)

    def test_empty_WITHOUT_a_pr_still_demands_the_pr(self):
        rc, err = self._run(["--assert-head", ""])
        self.assertEqual(rc, 2, err)
        self.assertIn("needs the PR number", err)

    def test_ABSENT_is_still_absent(self):
        """The arm: `is not None` must not turn a missing flag into a check."""
        rc, err = self._run(["--assert-head", "abcdef", "3781"])
        self.assertEqual(rc, 2, err)          # too short, i.e. the branch ran
        self.assertNotIn("needs the PR number", err)


class SuffixBypass(unittest.TestCase):
    """A head-plus-suffix must not read as unchanged.

    The compare was symmetric — `head.startswith(arg) or arg.startswith(head)` —
    so any value that merely BEGAN with the real head was accepted, which is the
    one shape a wrong sha most plausibly takes (a truncation, a paste of two
    shas, a sha with trailing junk). The guard exists to refuse a moved head; a
    guard that accepts a superstring of the head refuses nothing in that case.
    """

    def test_head_plus_suffix_is_REFUSED(self):
        rc, _, err = _run(["3781", "--assert-head", HEAD + "deadbeef"], HEAD)
        self.assertEqual(rc, 2, err)
        self.assertIn("not a sha", err)

    def test_directionality_the_arg_must_ABBREVIATE_the_head(self):
        """Pin the direction itself: a short head against a longer valid-hex arg
        is MOVED, never a match. The symmetric form passed this."""
        rc, _, err = _run(["3781", "--assert-head", "abcdef1234"], "abcdef12")
        self.assertEqual(rc, 3, err)
        self.assertIn("MOVED", err)

    def test_non_hex_is_a_usage_error(self):
        for bad in ("zzzzzzzz", "not-a-sha", HEAD[:-1] + "g"):
            rc, _, err = _run(["3781", "--assert-head", bad], HEAD)
            self.assertEqual(rc, 2, f"{bad!r}: {err}")
            self.assertIn("not a sha", err)

    def test_UPPERCASE_sha_still_matches(self):
        """Refusing non-hex must not start refusing a legitimately-cased sha."""
        rc, out, err = _run(["3781", "--assert-head", HEAD.upper()], HEAD)
        self.assertEqual(rc, 0, err)
        self.assertIn("unchanged", out)

    def test_exactly_40_is_still_accepted(self):
        """The length boundary, so >40 cannot drift into >=40."""
        rc, out, err = _run(["3781", "--assert-head", HEAD], HEAD)
        self.assertEqual(rc, 0, err)
        self.assertIn("unchanged", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
