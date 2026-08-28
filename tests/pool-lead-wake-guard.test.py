#!/usr/bin/env python3
"""Host-sleep reclaim guard through the production PoolLead entrypoints.

Run: python3 tests/pool-lead-wake-guard.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_follower import LEAD_STALE_S  # noqa: E402
from pool_lead import PoolLead  # noqa: E402


class WakeGuardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        self.results = root / "results"
        for path in (self.tasks, self.state, self.results):
            path.mkdir()
        self.pool = ["core-1", "core-2"]
        self.alive = {"core-1": True, "core-2": True}
        self.wall = 1_000.0
        self.mono = 500.0
        self.lead = PoolLead(
            self.tasks, self.state,
            followers_fn=lambda: list(self.pool),
            alive_fn=lambda instance: self.alive.get(instance, False),
            now_fn=lambda: self.wall,
            mono_fn=lambda: self.mono,
            results_dir=self.results)

    def tearDown(self):
        self.tmp.cleanup()

    def _sleep(self, seconds=968):
        self.alive = {instance: False for instance in self.pool}
        self.wall += seconds

    def _tick_until(self, target, fn):
        out = []
        while self.wall < target:
            self.wall += 2
            self.mono += 2
            out += fn()
        return out


class HostSleepDefersReclaim(WakeGuardBase):
    def test_claim_survives_sleep_until_owner_rebeats(self):
        claim = self.tasks / "task-a.claimed-core-2.txt"
        claim.write_text("x")
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self._sleep()
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.alive["core-2"] = True
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.assertTrue(claim.exists())

    def test_assignment_survives_sleep(self):
        assignment = self.tasks / "task-b.assigned-core-1.txt"
        assignment.write_text("x")
        self.assertEqual(self.lead.reclaim_dead(), [])
        self._sleep()
        self.assertEqual(self.lead.reclaim_dead(), [])
        self.assertTrue(assignment.exists())

    def test_sibling_rebeat_does_not_end_grace(self):
        claim = self.tasks / "task-c.claimed-core-2.txt"
        claim.write_text("x")
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self._sleep()
        self.alive["core-1"] = True
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.assertTrue(claim.exists())

    def test_stale_claim_repools_after_grace(self):
        claim = self.tasks / "task-d.claimed-core-2.txt"
        claim.write_text("x")
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self._sleep()
        self.assertEqual(self.lead.reclaim_claimed(), [])
        out = self._tick_until(self.wall + LEAD_STALE_S + 4,
                               self.lead.reclaim_claimed)
        self.assertEqual(out,
                         [("task-d.claimed-core-2.txt", "repooled")])


class ColdStartStaggeredWake(WakeGuardBase):
    """A lead started cold has no skew to read. After a host resume the
    ordinary shape is one follower re-beaten and one not yet, and reading the
    second as dead repools a claim its owner is still executing."""

    def _stagger(self):
        self.alive = {"core-1": True, "core-2": False}

    def test_stale_claimant_survives_the_first_tick(self):
        claim = self.tasks / "task-s.claimed-core-2.txt"
        claim.write_text("x")
        self._stagger()
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.assertTrue(claim.exists())
        self.assertFalse((self.tasks / "task-s.txt").exists())

    def test_stale_assignment_survives_the_first_tick(self):
        assignment = self.tasks / "task-t.assigned-core-2.txt"
        assignment.write_text("x")
        self._stagger()
        self.assertEqual(self.lead.reclaim_dead(), [])
        self.assertTrue(assignment.exists())

    def test_rebeat_inside_the_window_keeps_the_claim(self):
        claim = self.tasks / "task-u.claimed-core-2.txt"
        claim.write_text("x")
        self._stagger()
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.alive["core-2"] = True
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.assertTrue(claim.exists())

    def test_a_follower_that_never_returns_is_recovered_after_the_window(self):
        claim = self.tasks / "task-w.claimed-core-2.txt"
        claim.write_text("x")
        self._stagger()
        self.assertEqual(self.lead.reclaim_claimed(), [])
        out = self._tick_until(self.wall + LEAD_STALE_S + 4,
                               self.lead.reclaim_claimed)
        self.assertEqual(out, [("task-w.claimed-core-2.txt", "repooled")])

    def test_all_fresh_first_tick_opens_no_window(self):
        # Control: the deferral is caused by the stale follower, not by being
        # the first tick. With every follower proven, a later death is
        # recovered on the spot.
        self.assertEqual(self.lead.reclaim_claimed(), [])
        (self.tasks / "task-v.claimed-core-2.txt").write_text("x")
        self.alive["core-2"] = False
        self.wall += 300
        self.mono += 300
        self.assertEqual(self.lead.reclaim_claimed(),
                         [("task-v.claimed-core-2.txt", "repooled")])


class RecoveryStillRuns(WakeGuardBase):
    def test_one_dead_follower_is_recovered_after_the_cold_window(self):
        (self.tasks / "task-e.claimed-core-2.txt").write_text("x")
        self.alive["core-2"] = False
        self.assertEqual(self.lead.reclaim_claimed(), [])
        out = self._tick_until(self.wall + LEAD_STALE_S + 4,
                               self.lead.reclaim_claimed)
        self.assertEqual(out, [("task-e.claimed-core-2.txt", "repooled")])

    def test_empty_pool_does_not_defer(self):
        (self.tasks / "task-f.claimed-core-9.txt").write_text("x")
        self.pool = []
        self.assertEqual(
            self.lead.reclaim_claimed(),
            [("task-f.claimed-core-9.txt", "repooled")])

    def test_broken_follower_resolver_does_not_defer(self):
        def fail():
            raise OSError("registry unreadable")

        self.lead.followers_fn = fail
        self.alive["core-2"] = False
        (self.tasks / "task-g.claimed-core-2.txt").write_text("x")
        self.assertEqual(
            self.lead.reclaim_claimed(),
            [("task-g.claimed-core-2.txt", "repooled")])

    def test_slow_awake_daemon_does_not_get_sleep_grace(self):
        (self.tasks / "task-h.claimed-core-2.txt").write_text("x")
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.alive["core-2"] = False
        self.wall += 300
        self.mono += 300
        self.assertEqual(
            self.lead.reclaim_claimed(),
            [("task-h.claimed-core-2.txt", "repooled")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
