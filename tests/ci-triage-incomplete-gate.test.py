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
CT_PATH = REPO / "scripts" / "ci-triage.py"

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


class TestIncompletePredicate(unittest.TestCase):
    def setUp(self):
        self.ct = _load()

    def test_a_pending_statuscontext_is_incomplete(self):
        # The case a CheckRun-shaped filter cannot see: no `status` key at all.
        self.assertNotIn("status", PENDING_CONTEXT)
        self.assertTrue(self.ct._is_incomplete(PENDING_CONTEXT))

    def test_a_green_statuscontext_is_not(self):
        self.assertFalse(self.ct._is_incomplete(GREEN_CONTEXT))

    def test_a_running_checkrun_is_incomplete(self):
        self.assertTrue(self.ct._is_incomplete(RUNNING_CHECKRUN))

    def test_completed_checkruns_are_not_incomplete_either_way(self):
        # Not-incomplete is about COMPLETION, not about passing: a failed run
        # is complete, and `_is_bad` is what reports it.
        self.assertFalse(self.ct._is_incomplete(GREEN_CHECKRUN))
        self.assertFalse(self.ct._is_incomplete(FAILED_CHECKRUN))

    def test_the_running_checkrun_conclusion_is_empty_string_not_none(self):
        # Pinned so a `conclusion != None` filter is a visible mistake:
        # a RUNNING CheckRun carries `''`, which that test lets through.
        self.assertEqual(RUNNING_CHECKRUN["conclusion"], "")
        self.assertIsNotNone(RUNNING_CHECKRUN["conclusion"])
        self.assertFalse(self.ct._is_bad(RUNNING_CHECKRUN))


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

    def test_an_unreadable_rollup_is_unknown_not_ready(self):
        def fake_gh(run, args):
            if "statusCheckRollup" not in args:
                return {}
            fake_gh.n += 1
            return {"statusCheckRollup": [GREEN_CHECKRUN]} if fake_gh.n == 1 else None
        fake_gh.n = 0
        self.ct._gh = fake_gh
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.ct.main(["3698", "--repo", "o/r"])
        self.assertIn("UNKNOWN", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
