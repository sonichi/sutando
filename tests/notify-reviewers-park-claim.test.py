#!/usr/bin/env python3
"""Two concurrent callers must not both claim one reviewer's park.

Sequential cases cannot see this: check-then-append passes every one of them and
still lets two processes read "not parked", both append, and both post.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py"
MSG = "re-review https://github.com/sonichi/sutando/pull/3509"

# The child calls the PRODUCTION claim, not a re-implementation of it: a copy
# would pass while the shipped writer raced.
CHILD = f"""
import importlib.util, json, os, sys, time
spec = importlib.util.spec_from_file_location("nr", {str(SCRIPT)!r})
nr = importlib.util.module_from_spec(spec)
sys.path.insert(0, {str(REPO / "src")!r})
spec.loader.exec_module(nr)
gate = sys.argv[1]
while not os.path.exists(gate):
    time.sleep(0.002)
try:
    got = nr.claim_park({MSG!r}, "k", "k")
except OSError as e:
    got = "error:" + str(e)
print(json.dumps({{"claim": got}}))
"""


def _load():
    spec = importlib.util.spec_from_file_location("nr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "src"))
    spec.loader.exec_module(mod)
    return mod


class OneClaimWins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = pathlib.Path(self.tmp) / "asks.jsonl"
        self.gate = pathlib.Path(self.tmp) / "go"
        self.prev = os.environ.get("SUTANDO_REVIEW_ASKS_LEDGER")
        os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(self.led)

    def tearDown(self):
        if self.prev is None:
            os.environ.pop("SUTANDO_REVIEW_ASKS_LEDGER", None)
        else:
            os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = self.prev

    def _race(self, n=2):
        src = pathlib.Path(self.tmp) / "child.py"
        src.write_text(CHILD)
        procs = [subprocess.Popen([sys.executable, str(src), str(self.gate)],
                                  stdout=subprocess.PIPE, text=True,
                                  env=dict(os.environ)) for _ in range(n)]
        time.sleep(0.25)          # let every child reach the gate first
        self.gate.write_text("go")
        return [json.loads(p.communicate()[0] or '{"claim": null}')["claim"]
                for p in procs]

    def test_exactly_one_of_two_concurrent_callers_claims_the_park(self):
        claims = self._race()
        won = [c for c in claims if isinstance(c, int) and c]
        lost = [c for c in claims if c is None]
        self.assertEqual(len(won), 1, f"both callers claimed: {claims}")
        self.assertEqual(len(lost), 1, f"nobody was refused: {claims}")

    def test_the_race_leaves_exactly_one_pending_row(self):
        self._race()
        rows = [json.loads(l) for l in self.led.read_text().splitlines()]
        pending = [r for r in rows if r["outcome"] == "pending"]
        self.assertEqual(len(pending), 1, f"two reservations for one actor: {rows}")

    def test_the_control_a_serial_second_call_is_also_refused(self):
        # Proves the assertion above is about the CLAIM and not about the two
        # children failing to start: the same refusal holds without any race.
        nr = _load()
        first = nr.claim_park(MSG, "k", "k")
        second = nr.claim_park(MSG, "k", "k")
        self.assertTrue(first)
        self.assertIsNone(second)

    def test_the_negative_control_a_released_claim_is_reclaimable(self):
        # Without this, a claim that refused EVERYTHING would pass every case
        # above while suppressing legitimate retries.
        nr = _load()
        self.assertTrue(nr.claim_park(MSG, "k", "k"))
        nr.record_asks(MSG, "k", outcome="failed", actor="k")
        self.assertTrue(nr.claim_park(MSG, "k", "k"),
                        "a released reservation must be claimable again")


if __name__ == "__main__":
    unittest.main(verbosity=2)
