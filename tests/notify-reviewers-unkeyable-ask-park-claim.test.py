#!/usr/bin/env python3
"""An ask that claimed no park must not report that one holds.

The Matrix path passes require_ref=False, so a message with no PR URL returns
from reserve_ask BEFORE the ledger -- deliberately, since nothing can key it.
The settlement's OSError branch still announced "the park holds, so a repeat is
blocked", which is false in both halves: no park was claimed, and re-running
will send again. record_asks logs only full PR URLs, so no row exists either.
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_LED = Path(tempfile.mkdtemp()) / "ledger.jsonl"
os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(_LED)
_spec = importlib.util.spec_from_file_location(
    "nr", ROOT / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")
nr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nr)

TARGET = {"name": "alice", "endpoint": "@alice:x"}
ROSTER = {"alice": {"stand": "@alice:x", "room": "!r:x", "allowlisted": True}}
NO_PR = "please re-review the room change"
WITH_PR = "re-review https://github.com/o/r/pull/7"


def _warn(message, parked):
    """The stderr a failed settlement emits for this ask."""
    a = types.SimpleNamespace(kind="ask", message=message)
    saved = nr.record_asks

    def boom(*_a, **_k):
        raise OSError("disk full")
    buf = io.StringIO()
    try:
        nr.record_asks = boom
        with contextlib.redirect_stderr(buf):
            nr.settler(a, TARGET, "alice", parked=parked)("unknown", "timeout")
    finally:
        nr.record_asks = saved
    return buf.getvalue()


class AnUnkeyableAskHoldsNoPark(unittest.TestCase):
    def test_an_unkeyable_ask_reserves_nothing(self):
        a = types.SimpleNamespace(kind="ask", message=NO_PR)
        before = _LED.read_text().count("\n") if _LED.exists() else 0
        proceed, bucket, _note = nr.reserve_ask(
            a, TARGET, "alice", lambda w: w, ROSTER, require_ref=False)
        after = _LED.read_text().count("\n") if _LED.exists() else 0
        self.assertTrue(proceed)
        self.assertIsNone(bucket)
        self.assertEqual(after, before, "an unkeyable ask wrote a ledger row")

    def test_it_does_not_claim_a_park_that_was_never_taken(self):
        out = _warn(NO_PR, parked=False)
        self.assertNotIn("the park holds", out)
        self.assertIn("NOT blocked", out)
        self.assertIn("no PR URL", out)

    def test_the_control_a_keyed_ask_still_reports_the_park(self):
        # The honest message must survive: this branch really does block a repeat.
        out = _warn(WITH_PR, parked=True)
        self.assertIn("the park holds", out)
        self.assertIn("blocked until it is cleared", out)

    def test_the_control_a_notice_settles_silently_either_way(self):
        for parked in (True, False):
            a = types.SimpleNamespace(kind="notice", message=NO_PR)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = nr.settler(a, TARGET, "alice", parked=parked)("unknown", "t")
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
