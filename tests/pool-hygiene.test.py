#!/usr/bin/env python3
"""Pool liveness hygiene (L8): stuck assignments repool only when old AND the
follower is alive (dead is reclaim_dead's path), and done-flag pruning drops
only aged flags whose task has left tasks/ entirely.

Run: python3 tests/pool-hygiene.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_lead import PoolLead  # noqa: E402


class HygieneBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        self.tasks.mkdir()
        self.state.mkdir()
        self.alive = {"core-1": True, "core-2": True}
        self.clock = [10_000.0]
        self.lead = PoolLead(self.tasks, self.state,
                             followers_fn=lambda: list(self.alive),
                             alive_fn=lambda i: self.alive.get(i, False),
                             now_fn=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()


class StuckAssignmentTests(HygieneBase):
    def test_first_sight_adopts_then_old_age_repools(self):
        f = self.tasks / "task-s1.assigned-core-1.txt"
        f.write_text("task: t\n")
        self.assertEqual(self.lead.reclaim_stuck_assignments(), [])  # adopted
        self.clock[0] += 301
        self.assertEqual(self.lead.reclaim_stuck_assignments(),
                         ["task-s1.assigned-core-1.txt"])
        self.assertTrue((self.tasks / "task-s1.txt").exists())

    def test_young_assignment_untouched(self):
        (self.tasks / "task-s2.assigned-core-1.txt").write_text("task: t\n")
        self.lead.reclaim_stuck_assignments()
        self.clock[0] += 299
        self.assertEqual(self.lead.reclaim_stuck_assignments(), [])

    def test_dead_follower_left_to_reclaim_dead(self):
        (self.tasks / "task-s3.assigned-core-1.txt").write_text("task: t\n")
        self.lead.reclaim_stuck_assignments()
        self.alive["core-1"] = False
        self.clock[0] += 301
        self.assertEqual(self.lead.reclaim_stuck_assignments(), [])

    def test_claimed_files_never_touched(self):
        (self.tasks / "task-s4.claimed-core-1.txt").write_text("task: t\n")
        self.lead.reclaim_stuck_assignments()
        self.clock[0] += 10_000
        self.assertEqual(self.lead.reclaim_stuck_assignments(), [])
        self.assertTrue((self.tasks / "task-s4.claimed-core-1.txt").exists())

    def test_ledger_forgets_departed_files(self):
        f = self.tasks / "task-s5.assigned-core-1.txt"
        f.write_text("task: t\n")
        self.lead.reclaim_stuck_assignments()
        f.rename(self.tasks / "task-s5.claimed-core-1.txt")  # follower claimed
        self.clock[0] += 400
        self.assertEqual(self.lead.reclaim_stuck_assignments(), [])
        import json
        ledger = json.loads(
            (self.state / "pool" / "assignments.json").read_text())
        self.assertNotIn("task-s5.assigned-core-1.txt", ledger)


class PruneTests(HygieneBase):
    def _flag(self, core, stem, age_s):
        d = self.state / "cores" / core / "done"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{stem}.flag"
        p.touch()
        import os
        old = self.clock[0] - age_s
        os.utime(p, (old, old))
        return p

    def test_old_flag_of_archived_task_removed(self):
        p = self._flag("core-1", "task-p1", 8 * 86400)
        self.assertEqual(self.lead.prune_done_flags(), 1)
        self.assertFalse(p.exists())

    def test_old_flag_of_live_task_kept(self):
        p = self._flag("core-1", "task-p2", 8 * 86400)
        (self.tasks / "task-p2.claimed-core-1.txt").write_text("x")
        self.assertEqual(self.lead.prune_done_flags(), 0)
        self.assertTrue(p.exists())

    def test_young_flag_kept(self):
        p = self._flag("core-1", "task-p3", 3600)
        self.assertEqual(self.lead.prune_done_flags(), 0)
        self.assertTrue(p.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
