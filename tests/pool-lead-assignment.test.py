#!/usr/bin/env python3
"""PoolLead (L1): priority-ordered assignment, sticky affinity with idle
rebalance, least-loaded fallback, dead-follower reclaim, no-follower inertness.

Run: python3 tests/pool-lead-assignment.test.py   (stdlib only)
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_lead import AFFINITY_IDLE_S, PoolLead  # noqa: E402


class PoolLeadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        self.tasks.mkdir()
        self.state.mkdir()
        self.alive = {"core-a": True, "core-b": True}
        self.clock = [1000.0]
        self.lead = PoolLead(
            self.tasks, self.state,
            followers_fn=lambda: list(self.alive),
            alive_fn=lambda i: self.alive.get(i, False),
            now_fn=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, name, channel=None, priority=None):
        lines = [f"id: {name[:-4]}"]
        if channel:
            lines.append(f"channel_id: {channel}")
        if priority:
            lines.append(f"priority: {priority}")
        (self.tasks / name).write_text("\n".join(lines) + "\ntask: t\n")

    def _names(self):
        return sorted(f.name for f in self.tasks.iterdir())

    def test_urgent_assigned_before_low_backlog(self):
        self._task("task-low1.txt", priority="low")
        self._task("task-low2.txt", priority="low")
        self._task("task-hot.txt", priority="urgent")
        order = [n for n, _ in self.lead.sweep()]
        self.assertEqual(order[0], "task-hot.txt")
        self.assertEqual(len(order), 3)
        self.assertFalse([n for n in self._names()
                          if n.endswith(".txt") and ".assigned-" not in n])

    def test_channel_sticks_to_its_handler(self):
        self._task("task-c1.txt", channel="C123")
        first = dict(self.lead.sweep())["task-c1.txt"]
        self._task("task-c2.txt", channel="C123")
        # load-balance would pick the OTHER core; affinity must override
        second = dict(self.lead.sweep())["task-c2.txt"]
        self.assertEqual(second, first)

    def test_idle_channel_rebalances(self):
        self._task("task-c1.txt", channel="C123")
        first = dict(self.lead.sweep())["task-c1.txt"]
        for f in list(self.tasks.iterdir()):
            f.unlink()  # handler finished everything
        self.clock[0] += AFFINITY_IDLE_S + 1
        self._task("task-c2.txt", channel="C123")
        second = dict(self.lead.sweep())["task-c2.txt"]
        self.assertIn(second, self.alive)
        # no inequality assert: a rebalance may re-pick the same core by
        # load; only the stickiness must be gone
        row = self.lead._load_affinity()["C123"]
        self.assertEqual(row["ts"], self.clock[0])

    def test_least_loaded_gets_channelless_work(self):
        (self.tasks / "task-old.assigned-core-a.txt").write_text("x")
        self._task("task-new.txt")
        inst = dict(self.lead.sweep())["task-new.txt"]
        self.assertEqual(inst, "core-b")

    def test_dead_follower_assignments_reclaimed_claims_kept(self):
        (self.tasks / "task-r1.assigned-core-a.txt").write_text("x")
        (self.tasks / "task-r2.claimed-core-a.txt").write_text("x")
        self.alive["core-a"] = False
        reclaimed = self.lead.reclaim_dead()
        self.assertEqual(reclaimed, ["task-r1.assigned-core-a.txt"])
        self.assertIn("task-r1.txt", self._names())
        self.assertIn("task-r2.claimed-core-a.txt", self._names())

    def test_no_live_followers_leaves_tasks_untouched(self):
        self._task("task-x.txt")
        self.alive.clear()
        self.assertEqual(self.lead.sweep(), [])
        self.assertIn("task-x.txt", self._names())

    def test_affinity_to_dead_follower_reassigns(self):
        self._task("task-c1.txt", channel="C9")
        pinned = dict(self.lead.sweep())["task-c1.txt"]
        self.alive[pinned] = False
        self._task("task-c2.txt", channel="C9")
        inst = dict(self.lead.sweep())["task-c2.txt"]
        self.assertNotEqual(inst, pinned)

    def test_assigned_file_never_reassigned(self):
        # an assigned name must not re-enter the pending set — the id
        # charset contains dots, so this once double-assigned (L1 bug)
        (self.tasks / "task-old.assigned-core-a.txt").write_text("x")
        self.assertEqual(self.lead.sweep(), [])
        self.assertEqual(self._names(), ["task-old.assigned-core-a.txt"])




class ReclaimClaimedTest(unittest.TestCase):
    """Crash-mid-claim recovery: done-flag discriminates delivered vs repooled."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tasks = self.tmp / "tasks"
        self.state = self.tmp / "state"
        self.tasks.mkdir()
        (self.state / "cores").mkdir(parents=True)
        self.lead = PoolLead(
            self.tasks, self.state,
            followers_fn=lambda: ["core-1"],
            alive_fn=lambda inst: inst == "core-1",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _claimed(self, tid, inst):
        f = self.tasks / f"task-{tid}.claimed-{inst}.txt"
        f.write_text(f"id: task-{tid}\n")
        return f

    def test_dead_claimer_without_done_flag_repools(self):
        self._claimed("x1", "core-9")
        out = self.lead.reclaim_claimed()
        self.assertEqual(out, [("task-x1.claimed-core-9.txt", "repooled")])
        self.assertTrue((self.tasks / "task-x1.txt").exists())

    def test_dead_claimer_with_done_flag_restores_for_delivery_only(self):
        self._claimed("x2", "core-9")
        done = self.state / "cores" / "core-9" / "done"
        done.mkdir(parents=True)
        (done / "task-x2.flag").write_text("")  # producer convention: stem + .flag
        out = self.lead.reclaim_claimed()
        self.assertEqual(out, [("task-x2.claimed-core-9.txt", "delivered")])
        self.assertTrue((self.tasks / "task-x2.txt").exists())
        self.assertEqual(self.lead.sweep(), [])  # never reassigned

    def test_live_claimer_untouched(self):
        f = self._claimed("x3", "core-1")
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.assertTrue(f.exists())

    def test_repooled_task_is_reassignable(self):
        self._claimed("x4", "core-9")
        self.lead.reclaim_claimed()
        assigned = self.lead.sweep()
        self.assertEqual(assigned, [("task-x4.txt", "core-1")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
