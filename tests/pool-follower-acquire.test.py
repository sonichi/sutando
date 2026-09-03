#!/usr/bin/env python3
"""pool_follower (L2): assignment honoring, no-steal under a live lead,
leaderless fallback on a stale/absent/future-dated lead beat.

Run: python3 tests/pool-follower-acquire.test.py   (stdlib only)
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import pool_follower as pf  # noqa: E402

sys.path.insert(0, str(REPO / "src" / "runtime-api"))
from pool_metrics import PoolMetrics  # noqa: E402


class AcquireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        (self.state / "cores").mkdir(parents=True)
        self.tasks.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _beat(self, label="lead", age=0):
        f = self.state / "cores" / f"{label}.alive"
        f.write_text("{}")
        t = time.time() - age
        os.utime(f, (t, t))

    def test_own_assignment_claimed_in_priority_order(self):
        (self.tasks / "task-a.assigned-me.txt").write_text("priority: low\n")
        (self.tasks / "task-b.assigned-me.txt").write_text("priority: urgent\n")
        self._beat()
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-b.claimed-me.txt")

    def test_other_followers_assignment_never_touched(self):
        (self.tasks / "task-a.assigned-peer.txt").write_text("x")
        self._beat()
        self.assertIsNone(pf.acquire_work(self.tasks, self.state, "me", "lead"))
        self.assertTrue((self.tasks / "task-a.assigned-peer.txt").exists())

    def test_live_lead_owns_the_unassigned_pool(self):
        (self.tasks / "task-free.txt").write_text("task: t\n")
        self._beat(age=10)
        self.assertIsNone(pf.acquire_work(self.tasks, self.state, "me", "lead"))
        self.assertTrue((self.tasks / "task-free.txt").exists())

    def test_stale_lead_falls_back_to_leaderless_claim(self):
        (self.tasks / "task-free.txt").write_text("task: t\n")
        self._beat(age=pf.LEAD_STALE_S + 5)
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-free.claimed-me.txt")

    def test_absent_lead_beat_also_falls_back(self):
        (self.tasks / "task-free.txt").write_text("task: t\n")
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-free.claimed-me.txt")

    def test_fallback_claim_lands_in_the_lead_metrics(self):
        """The production claim path is the only producer of fallback_claims;
        the summary the status view reads must move when it fires."""
        (self.tasks / "task-free.txt").write_text("task: t\n")
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-free.claimed-me.txt")
        s = PoolMetrics(self.state).summarize()
        self.assertEqual(s["fallback_claims"], 1)
        self.assertEqual(s["rows"], 1)

    def test_assignment_claim_is_recorded_but_is_not_a_fallback(self):
        (self.tasks / "task-a.assigned-me.txt").write_text("task: t\n")
        self._beat()
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-a.claimed-me.txt")
        s = PoolMetrics(self.state).summarize()
        self.assertEqual((s["rows"], s["fallback_claims"]), (1, 0))

    def test_future_dated_lead_beat_degrades(self):
        # clock skew: a lead "from the future" is not a live lead
        f = self.state / "cores" / "lead.alive"
        f.write_text("{}")
        t = time.time() + 3600
        os.utime(f, (t, t))
        (self.tasks / "task-free.txt").write_text("task: t\n")
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-free.claimed-me.txt")

    def test_fallback_skips_task_with_result_evidence(self):
        # A reclaimed task with an existing result must not be re-executed:
        # the fallback path needs the lead's pooling-scan guard.
        root = self.tasks.parent
        for disposition in ("", "archive", "undelivered"):
            name = f"task-done-{disposition or 'live'}"
            (self.tasks / f"{name}.txt").write_text("task: t\n")
            rdir = root / "results" / disposition if disposition else root / "results"
            rdir.mkdir(parents=True, exist_ok=True)
            (rdir / f"{name}.txt").write_text("already answered\n")
        (self.tasks / "task-fresh.txt").write_text("task: t\n")
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-fresh.claimed-me.txt")
        for disposition in ("live", "archive", "undelivered"):
            self.assertTrue((self.tasks / f"task-done-{disposition}.txt").exists(),
                            f"{disposition}: evidenced task must stay put, unclaimed")
        self.assertIsNone(pf.acquire_work(self.tasks, self.state, "me", "lead"),
                          "only evidenced tasks remain -> idle, never a re-claim")

    def test_assignments_still_honored_in_fallback_mode(self):
        (self.tasks / "task-mine.assigned-me.txt").write_text("x")
        (self.tasks / "task-free.txt").write_text("task: t\n")
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-mine.claimed-me.txt")

    def test_fallback_never_takes_claimed_or_assigned_files(self):
        (self.tasks / "task-x.claimed-peer.txt").write_text("x")
        (self.tasks / "task-y.assigned-peer.txt").write_text("x")
        self.assertIsNone(pf.acquire_work(self.tasks, self.state, "me", "lead"))
        self.assertEqual(len(list(self.tasks.iterdir())), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
