#!/usr/bin/env python3
"""The one freshness and single-use gate every recovery entry path calls before it acts.

A command is admitted while fresh (issued within the TTL, not from the future) and only once;
admission is on disk before the caller acts, so a crash after it reports the command as
interrupted and never runs it again. Paths: the bare reply (core_limit_hold), the startup sweep
(the same function) and a card's tap (recovery_actions).

Run: python3 tests/recovery-gate.test.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import recovery_gate as G  # noqa: E402


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_admitted_once_while_fresh(self):
        self.assertEqual(G.admit(self.ws, "k", 1000, now=1000 + G.TTL_S), (True, None))
        self.assertEqual(G.admit(self.ws, "k", 1000, now=1001 + G.TTL_S), (False, "interrupted"),
                         "admitted but never finished: a crash, so it is never run again")
        G.finish(self.ws, "k", "done", now=1002)
        self.assertEqual(G.admit(self.ws, "k", 1000, now=1003), (False, "replay"))
        self.assertEqual(G.state(self.ws, "k")["outcome"], "done")

    def test_stale_future_and_malformed_stamps_are_refused(self):
        now = 5000
        for issued in (now - G.TTL_S - 1, now + G.FUTURE_SKEW_S + 1, None, float("nan"), True, str(now)):
            with self.subTest(issued=issued):
                self.assertEqual(G.admit(self.ws, f"k-{issued!r}", issued, now=now), (False, "stale"))
        self.assertEqual(G.admit(self.ws, "edge", now + G.FUTURE_SKEW_S, now=now), (True, None))
        self.assertTrue(G.fresh(1000, 1000 + G.TTL_S))
        self.assertFalse(G.fresh(1000, 1000 + G.TTL_S + 1))

    def test_the_claim_is_on_disk_before_the_caller_acts(self):
        G.admit(self.ws, "k", 5, now=6)
        [path] = list(G.claims_dir(self.ws).glob("*.json"))
        body = json.loads(path.read_text())
        self.assertEqual((body["key"], body["state"], body["issued_at"]), ("k", G.ACCEPTED, 5))
        self.assertNotIn("k", path.name, "the file is named by a digest, never by the key's text")

    def test_a_racing_second_admission_loses(self):
        G.admit(self.ws, "k", 5, now=6)
        real = G.state
        try:
            G.state = lambda ws, key: None  # both racers saw no claim; the exclusive create decides
            self.assertEqual(G.admit(self.ws, "k", 5, now=6), (False, "replay"))
        finally:
            G.state = real

    def test_old_finished_claims_are_pruned_and_live_ones_kept(self):
        G.admit(self.ws, "old", 5, now=6)
        G.finish(self.ws, "old", "done", now=7)
        G.admit(self.ws, "live", 5, now=6)
        import os
        for p in G.claims_dir(self.ws).glob("*.json"):
            os.utime(p, (1, 1))
        G.admit(self.ws, "new", G.KEEP_S + 10, now=G.KEEP_S + 10)
        self.assertIsNone(G.state(self.ws, "old"))
        self.assertIsNotNone(G.state(self.ws, "live"), "an unfinished claim is evidence of a crash: kept")
        (G.claims_dir(self.ws) / "junk.json").write_text("{")
        os.utime(G.claims_dir(self.ws) / "junk.json", (1, 1))
        G._prune(G.claims_dir(self.ws), G.KEEP_S + 20)  # unreadable files are skipped, never fatal

    def test_an_unreadable_claim_reads_as_none(self):
        G.claims_dir(self.ws).mkdir(parents=True)
        G._path(self.ws, "k").write_text("[1]")
        self.assertIsNone(G.state(self.ws, "k"))
        G.finish(self.ws, "fresh-key", "refused", now=1)
        self.assertEqual(G.state(self.ws, "fresh-key")["state"], G.FINISHED)


if __name__ == "__main__":
    unittest.main()
