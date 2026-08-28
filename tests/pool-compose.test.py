#!/usr/bin/env python3
"""L1+L2 compose: the lead's assignment names are exactly what the follower
claims — the file-name contract between the two modules, where drift bites.

Run: python3 tests/pool-compose.test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

import pool_follower as pf  # noqa: E402
from pool_lead import PoolLead  # noqa: E402


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        (self.state / "cores").mkdir(parents=True)
        self.tasks.mkdir()
        self.alive = {"f1": True, "f2": True}
        self.lead = PoolLead(self.tasks, self.state,
                             followers_fn=lambda: list(self.alive),
                             alive_fn=lambda i: self.alive.get(i, False))
        beat = self.state / "cores" / "lead.alive"
        beat.write_text("{}")

    def tearDown(self):
        self.tmp.cleanup()

    def test_assignment_flows_to_claim_end_to_end(self):
        (self.tasks / "task-w~1.txt").write_text(
            "id: task-w~1\nchannel_id: C7\ntask: t\n")
        assigned = dict(self.lead.sweep())
        inst = assigned["task-w~1.txt"]
        got = pf.acquire_work(self.tasks, self.state, inst, "lead")
        self.assertIsNotNone(got, "follower failed to claim its assignment")
        self.assertEqual(got.name, f"task-w~1.claimed-{inst}.txt")
        other = ({"f1", "f2"} - {inst}).pop()
        self.assertIsNone(
            pf.acquire_work(self.tasks, self.state, other, "lead"))

    def test_leads_load_counter_sees_follower_claims(self):
        # fallback-claimed work must weigh in the lead's next pick
        (self.tasks / "task-a.claimed-f1.txt").write_text("x")
        (self.tasks / "task-new.txt").write_text("task: t\n")
        inst = dict(self.lead.sweep())["task-new.txt"]
        self.assertEqual(inst, "f2")

    def test_lead_reclaim_feeds_follower_fallback(self):
        (self.tasks / "task-r.assigned-f1.txt").write_text("task: t\n")
        # A proven-alive tick first: a cold lead defers while any follower is
        # unproven, so the death below has to be a transition it witnessed.
        self.assertEqual(self.lead.reclaim_dead(), [])
        self.alive["f1"] = False
        self.lead.reclaim_dead()
        # lead dies too; surviving follower picks it up leaderless
        (self.state / "cores" / "lead.alive").unlink()
        got = pf.acquire_work(self.tasks, self.state, "f2", "lead")
        self.assertEqual(got.name, "task-r.claimed-f2.txt")

    def test_lead_records_assignment_metrics(self):
        sys.path.insert(0, str(REPO / "src" / "runtime-api"))
        from pool_metrics import PoolMetrics
        m = PoolMetrics(self.state, now_fn=lambda: 1_700_000_000.0)
        self.lead.metrics = m
        (self.tasks / "task-m1.txt").write_text("channel_id: C5\ntask: t\n")
        self.lead.sweep()
        s = m.summarize()
        self.assertEqual(sum(s["assignment_distribution"].values()), 1)
        self.assertIn("C5", s["mean_wait_by_channel"])

    def test_lead_records_reclaim_decisions_in_durable_metrics(self):
        from pool_metrics import PoolMetrics
        m = PoolMetrics(self.state, now_fn=lambda: 1_700_000_000.0)
        self.lead.metrics = m
        (self.tasks / "task-r1.assigned-f1.txt").write_text("x")
        (self.tasks / "task-r2.claimed-f1.txt").write_text("x")
        # Proven-alive tick first (see the cold-lead guard); records nothing.
        self.assertEqual(self.lead.reclaim_dead(), [])
        self.alive["f1"] = False
        self.lead.reclaim_dead()
        self.lead.reclaim_claimed()
        rows = [json.loads(line) for line in m._path().read_text().splitlines()]
        self.assertEqual(
            [(row["task"], row["dead_instance"], row["disposition"])
             for row in rows],
            [("task-r1.assigned-f1.txt", "f1", "assigned"),
             ("task-r2.claimed-f1.txt", "f1", "repooled")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
