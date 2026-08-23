#!/usr/bin/env python3
"""L1+L2 compose: the lead's assignment names are exactly what the follower
claims — the file-name contract between the two modules, where drift bites.

Run: python3 tests/pool-compose.test.py   (stdlib only)
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
        self.alive["f1"] = False
        self.lead.reclaim_dead()
        # lead dies too; surviving follower picks it up leaderless
        (self.state / "cores" / "lead.alive").unlink()
        got = pf.acquire_work(self.tasks, self.state, "f2", "lead")
        self.assertEqual(got.name, "task-r.claimed-f2.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
