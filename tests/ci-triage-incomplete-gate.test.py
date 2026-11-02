#!/usr/bin/env python3
"""`no failing checks` must not read as `ready` while something still gates.

`statusCheckRollup` is a UNION. A CheckRun carries `status`/`conclusion` and no
`state`; a StatusContext carries `state` and neither of the others. Measured on
sonichi/sutando#3814: 18 CheckRun + 1 StatusContext, and the StatusContext is
`license/cla` — the most common non-code blocker on this repo.

`_is_bad` already reads both shapes, so failures were never missed. What was
missed is the other direction: a PENDING `license/cla` is not failing, so the
tool printed a bare "no failing checks" while the merge stayed blocked. That is
the worst polarity of a mis-shaped filter — an item the classifier never
enumerated produces a confident all-clear with no artifact to be suspicious of.
(Union shape reported by @yixuan-ag2; both field layouts re-measured here.)

Run: python3 tests/ci-triage-incomplete-gate.test.py
"""
import importlib.util
import io
import contextlib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CT_PATH = REPO / "skills" / "review-preflight" / "scripts" / "ci-triage.py"

if not CT_PATH.exists():
    raise SystemExit(f"ci-triage.py not found at {CT_PATH} — refusing to report "
                     "a green run in which no test executed")


def _load():
    spec = importlib.util.spec_from_file_location("ci_triage", CT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Exactly the shapes gh emits, keys included and omitted as measured.
RUNNING_CHECKRUN = {"__typename": "CheckRun", "name": "python standalone tests",
                    "status": "IN_PROGRESS", "conclusion": "",
                    "startedAt": "2026-09-03T15:48:33Z",
                    "completedAt": "0001-01-01T00:00:00Z"}
GREEN_CHECKRUN = {"__typename": "CheckRun", "name": "ruff over Python sources",
                  "status": "COMPLETED", "conclusion": "SUCCESS",
                  "startedAt": "2026-09-03T15:48:33Z",
                  "completedAt": "2026-09-03T15:49:00Z"}
FAILED_CHECKRUN = {"__typename": "CheckRun", "name": "tsc + tests",
                   "status": "COMPLETED", "conclusion": "FAILURE",
                   "startedAt": "2026-09-03T15:48:33Z",
                   "completedAt": "2026-09-03T15:49:00Z"}
PENDING_CONTEXT = {"__typename": "StatusContext", "context": "license/cla",
                   "state": "PENDING", "startedAt": "2026-09-03T15:47:39Z"}
GREEN_CONTEXT = {"__typename": "StatusContext", "context": "license/cla",
                 "state": "SUCCESS", "startedAt": "2026-09-03T15:47:39Z"}
EXPECTED_CONTEXT = {"__typename": "StatusContext", "context": "required/never-reported",
                    "state": "EXPECTED", "startedAt": "2026-09-03T15:47:39Z"}
FAILED_CONTEXT = {"__typename": "StatusContext", "context": "license/cla",
                  "state": "FAILURE", "startedAt": "2026-09-03T15:47:39Z"}


class TestIncompletePredicate(unittest.TestCase):
    def setUp(self):
        self.ct = _load()

    def test_a_pending_statuscontext_is_incomplete(self):
        # The case a CheckRun-shaped filter cannot see: no `status` key at all.
        self.assertNotIn("status", PENDING_CONTEXT)
        self.assertTrue(self.ct._is_incomplete(PENDING_CONTEXT))

    def test_a_green_statuscontext_is_not(self):
        self.assertFalse(self.ct._is_incomplete(GREEN_CONTEXT))

    def test_expected_is_incomplete_too(self):
        # StatusState is EXPECTED/ERROR/FAILURE/PENDING/SUCCESS; enumerating
        # PENDING alone lets a never-reported required context read green.
        self.assertTrue(self.ct._is_incomplete(EXPECTED_CONTEXT))

    def test_failing_is_not_also_incomplete(self):
        # The two sets stay disjoint: `_is_bad` owns failure, and a red item
        # must not be double-reported as merely-not-green.
        self.assertTrue(self.ct._is_bad(FAILED_CONTEXT))
        self.assertFalse(self.ct._is_incomplete(FAILED_CONTEXT))

    def test_a_running_checkrun_is_incomplete(self):
        self.assertTrue(self.ct._is_incomplete(RUNNING_CHECKRUN))

    def test_completed_checkruns_are_not_incomplete_either_way(self):
        # Not-incomplete is about COMPLETION, not about passing: a failed run
        # is complete, and `_is_bad` is what reports it.
        self.assertFalse(self.ct._is_incomplete(GREEN_CHECKRUN))
        self.assertFalse(self.ct._is_incomplete(FAILED_CHECKRUN))

    def test_a_cancelled_checkrun_is_incomplete_not_clean(self):
        # `_is_bad` excludes CANCELLED, so not-incomplete too would mean a
        # completed-but-unsuccessful check renders as a clean rollup.
        c = {"name": "typecheck", "status": "COMPLETED", "conclusion": "CANCELLED"}
        self.assertFalse(self.ct._is_bad(c))
        self.assertTrue(self.ct._is_incomplete(c))

    def test_no_conclusion_can_fall_through_both_predicates(self):
        # The real invariant, asserted over the whole enum rather than the cases
        # someone remembered: every completed conclusion is green, bad, or incomplete.
        green = {"SUCCESS", "NEUTRAL", "SKIPPED"}
        for concl in ("SUCCESS", "NEUTRAL", "SKIPPED", "FAILURE", "TIMED_OUT",
                      "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE",
                      "SOME_CONCLUSION_GITHUB_ADDS_LATER"):
            c = {"name": "x", "status": "COMPLETED", "conclusion": concl}
            bad, inc = self.ct._is_bad(c), self.ct._is_incomplete(c)
            self.assertFalse(bad and inc, f"{concl} counted twice")
            if concl in green:
                self.assertFalse(bad or inc, f"{concl} should be clean")
            else:
                # Unknown conclusions land here too: fail-closed is the safe default.
                self.assertTrue(bad or inc, f"{concl} falls through both -> false all-clear")

    def test_the_running_checkrun_conclusion_is_empty_string_not_none(self):
        # Pinned so a `conclusion != None` filter is a visible mistake:
        # a RUNNING CheckRun carries `''`, which that test lets through.
        self.assertEqual(RUNNING_CHECKRUN["conclusion"], "")
        self.assertIsNotNone(RUNNING_CHECKRUN["conclusion"])
        self.assertFalse(self.ct._is_bad(RUNNING_CHECKRUN))


class TestIncompleteChecksApi(unittest.TestCase):
    """The `rollup=None` fetch path — `main` passes one, so only callers hit it."""

    def setUp(self):
        self.ct = _load()

    def test_it_fetches_when_no_rollup_is_supplied(self):
        seen = []

        def fake_gh(run, args):
            seen.append(args)
            return {"statusCheckRollup": [GREEN_CHECKRUN, PENDING_CONTEXT]}
        self.ct._gh = fake_gh
        got = self.ct.incomplete_checks("1", None, "o/r")
        self.assertEqual(got, ["license/cla"])
        self.assertEqual(len(seen), 1)

    def test_an_unreadable_fetch_returns_none_not_empty(self):
        # None means "could not tell"; [] would mean "nothing is gating" and
        # is the confident all-clear this whole PR exists to prevent.
        self.ct._gh = lambda run, args: None
        self.assertIsNone(self.ct.incomplete_checks("1", None, "o/r"))


class TestMainOutput(unittest.TestCase):
    def setUp(self):
        self.ct = _load()

    def _main_with(self, rollup):
        def fake_gh(run, args):
            return {"statusCheckRollup": rollup} if "statusCheckRollup" in args else {}
        self.ct._gh = fake_gh
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.ct.main(["3698", "--repo", "o/r"])
        return buf.getvalue()

    def test_a_pending_cla_is_named_not_swallowed(self):
        out = self._main_with([GREEN_CHECKRUN, PENDING_CONTEXT])
        self.assertIn("license/cla", out)
        self.assertIn("still gated", out)

    def test_an_all_green_pr_still_gets_the_plain_all_clear(self):
        out = self._main_with([GREEN_CHECKRUN, GREEN_CONTEXT])
        self.assertIn("no failing checks", out)
        self.assertNotIn("still gated", out)

    def test_a_running_check_is_named(self):
        out = self._main_with([GREEN_CHECKRUN, RUNNING_CHECKRUN])
        self.assertIn("python standalone tests", out)
        self.assertIn("still gated", out)

    def test_an_expected_context_is_named(self):
        out = self._main_with([GREEN_CHECKRUN, EXPECTED_CONTEXT])
        self.assertIn("required/never-reported", out)
        self.assertIn("still gated", out)

    def test_a_cancelled_required_check_is_named_not_all_clear(self):
        # The end-to-end form of the predicate fix: a cancelled required check
        # must reach the reviewer, not be rendered as a clean rollup.
        out = self._main_with([GREEN_CHECKRUN,
                               {"name": "typecheck", "status": "COMPLETED",
                                "conclusion": "CANCELLED"}])
        self.assertIn("typecheck", out)
        self.assertIn("still gated", out)

    def test_an_unreadable_rollup_is_unknown_not_ready(self):
        self.ct._gh = lambda run, args: None
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.ct.main(["3698", "--repo", "o/r"])
        self.assertIn("UNKNOWN", buf.getvalue())
        self.assertNotIn("no failing checks", buf.getvalue())

    def test_the_rollup_is_read_exactly_once(self):
        # `red` and `waiting` must describe the same instant, so main fetches
        # once and passes it down rather than re-reading.
        calls = []

        def fake_gh(run, args):
            if "statusCheckRollup" in args:
                calls.append(args)
                return {"statusCheckRollup": [GREEN_CHECKRUN, PENDING_CONTEXT]}
            return {}
        self.ct._gh = fake_gh
        with contextlib.redirect_stdout(io.StringIO()):
            self.ct.main(["3698", "--repo", "o/r"])
        self.assertEqual(len(calls), 1, f"rollup fetched {len(calls)}x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
