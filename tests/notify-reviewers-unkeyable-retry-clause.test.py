#!/usr/bin/env python3
"""A PR-less ask must not tell the operator that a park protects a repeat.

The Matrix path passes `require_ref=False`, so an ask with no full PR URL sends
without reserving anything -- `reserve_ask` returns before the ledger and
`record_asks` writes no row. Every unknown-outcome diagnostic on that path still
called `retry_clause(a.kind)`, which promises "the park holds, so a repeat is
refused" for any ask. Both halves of that are false there: nothing was claimed,
and re-running sends again.

The existing unkeyable suite exercises `settler()` with an injected exception, so
it never reaches these lines. This drives two complete `main()` invocations, which
is the only scope that sees the whole-command paths.

Run: python3 tests/notify-reviewers-unkeyable-retry-clause.test.py
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py"
_LED = Path(tempfile.mkdtemp(prefix="nr-retry-clause-")) / "ledger.jsonl"
os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(_LED)

NO_PR = "please take another look at the room change"
WITH_PR = "please re-review https://github.com/o/r/pull/7"
PARK_PROMISE = "the park holds"
ROSTER = {"alice": {"stand": "@alice:x", "room": "!r:x", "allowlisted": True},
          "bob": {"stand": "@bob:x", "room": "!r:x", "allowlisted": True}}


def _load():
    spec = importlib.util.spec_from_file_location("_nr_retry", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(message):
    """One whole `main()` --send whose room_ops call times out (an UNKNOWN)."""
    m = _load()
    m.load_roster = lambda: ROSTER
    m.stand_present_in_room = lambda t: (True, "2 members")
    m._github_login = lambda name, roster: (name, "stubbed")
    m.gate_capability = lambda repo, login: (True, "write")

    class _Sub:
        TimeoutExpired = m.subprocess.TimeoutExpired

        @staticmethod
        def run(*_a, **_k):
            raise _Sub.TimeoutExpired("room_ops", 60)

    m.subprocess = _Sub
    before = _LED.read_text().count("\n") if _LED.exists() else 0
    err = io.StringIO()
    argv = ["nr", "--reviewers", "alice,bob", "--kind", "ask", "--send",
            "--message", message]
    with patch.object(sys, "argv", argv), contextlib.redirect_stderr(err), \
            contextlib.redirect_stdout(io.StringIO()):
        rc = m.main()
    after = _LED.read_text().count("\n") if _LED.exists() else 0
    return rc, err.getvalue(), after - before


class AnUnkeyableAskPromisesNoPark(unittest.TestCase):
    def test_the_pr_less_run_never_claims_a_park_holds(self):
        rc, err, rows = _run(NO_PR)
        self.assertEqual(rows, 0, "an unkeyable ask must write no ledger row")
        self.assertNotIn(
            PARK_PROMISE, err,
            "no park was reserved and none can be, so promising one is false")

    def test_the_pr_less_run_says_a_repeat_may_duplicate(self):
        _rc, err, _rows = _run(NO_PR)
        self.assertIn("MAY duplicate", err,
                      "the operator must be told the repeat is unprotected")

    def test_the_keyable_control_still_reports_its_park(self):
        # Control: without this, deleting the promise everywhere would pass above.
        rc, err, rows = _run(WITH_PR)
        self.assertGreater(rows, 0, "a keyable ask must record its unknown")
        self.assertIn(PARK_PROMISE, err,
                      "a real reservation must still be reported")

    def test_both_runs_exit_unknown_so_the_difference_is_only_the_promise(self):
        # Pins that the two invocations differ in what they SAY, not in outcome.
        self.assertEqual(_run(NO_PR)[0], 4)
        self.assertEqual(_run(WITH_PR)[0], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
