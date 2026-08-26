#!/usr/bin/env python3
"""Wake guard: every follower going stale in the same instant is a host gap
(sleep, clock jump), not N deaths, so reclaim defers one stale window.

A 968s clamshell sleep aged every .alive file past the window at once; the lead
read that as four dead cores and repooled a live core's claim, bouncing an owner
task across cores for ~45 minutes. Exercises the production reclaim entrypoints.
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
        for d in (self.tasks, self.state, self.results):
            d.mkdir()
        self.pool = ["core-1", "core-2"]
        self.alive = {"core-1": True, "core-2": True}
        self.clock = 1_000.0
        self.lead = PoolLead(self.tasks, self.state,
                             followers_fn=lambda: list(self.pool),
                             alive_fn=lambda i: self.alive.get(i, False),
                             now_fn=lambda: self.clock,
                             results_dir=self.results)

    def tearDown(self):
        self.tmp.cleanup()

    def _sleep_whole_host(self, seconds):
        """What a clamshell sleep does: every beat ages together, clock jumps."""
        for inst in self.pool:
            self.alive[inst] = False
        self.clock += seconds

    def _names(self):
        return sorted(f.name for f in self.tasks.iterdir())


class HostGapDefersReclaim(WakeGuardBase):
    def test_claim_of_live_core_survives_a_host_sleep(self):
        (self.tasks / "task-a.claimed-core-2.txt").write_text("x")
        self._sleep_whole_host(968)
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.assertEqual(self._names(), ["task-a.claimed-core-2.txt"])

    def test_assignment_survives_a_host_sleep(self):
        (self.tasks / "task-b.assigned-core-1.txt").write_text("x")
        self._sleep_whole_host(968)
        self.assertEqual(self.lead.reclaim_dead(), [])
        self.assertEqual(self._names(), ["task-b.assigned-core-1.txt"])

    def test_stuck_sweep_defers_too(self):
        (self.tasks / "task-c.assigned-core-1.txt").write_text("x")
        self.lead.reclaim_stuck_assignments()  # adopt into the ledger
        self._sleep_whole_host(968)
        self.assertEqual(self.lead.reclaim_stuck_assignments(), [])
        self.assertEqual(self._names(), ["task-c.assigned-core-1.txt"])

    def test_woken_follower_clears_the_deferral(self):
        (self.tasks / "task-d.claimed-core-2.txt").write_text("x")
        self._sleep_whole_host(968)
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.alive["core-2"] = True          # beat resumes on wake
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.assertEqual(self._names(), ["task-d.claimed-core-2.txt"])


class GenuineFailuresStillReclaim(WakeGuardBase):
    def test_one_dead_core_reclaims_immediately(self):
        """The guard must not blunt the case it was never about."""
        (self.tasks / "task-e.claimed-core-2.txt").write_text("x")
        self.alive["core-2"] = False          # core-1 still beating
        self.assertEqual(self.lead.reclaim_claimed(),
                         [("task-e.claimed-core-2.txt", "repooled")])
        self.assertEqual(self._names(), ["task-e.txt"])

    def test_whole_pool_outage_reclaims_after_one_window(self):
        """Deferral, not suppression: a real total outage still recovers."""
        (self.tasks / "task-f.claimed-core-1.txt").write_text("x")
        self._sleep_whole_host(968)
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.clock += LEAD_STALE_S + 1        # still nothing beating
        self.assertEqual(self.lead.reclaim_claimed(),
                         [("task-f.claimed-core-1.txt", "repooled")])
        self.assertEqual(self._names(), ["task-f.txt"])

    def test_empty_pool_is_not_a_host_gap(self):
        """No followers means nothing to protect — never defer on an empty set."""
        (self.tasks / "task-g.claimed-core-9.txt").write_text("x")
        self.pool = []
        self.assertEqual(self.lead.reclaim_claimed(),
                         [("task-g.claimed-core-9.txt", "repooled")])

    def test_broken_followers_resolver_does_not_defer(self):
        """A resolver that raises must fail toward recovery, not toward stalling."""
        def boom():
            raise OSError("registry unreadable")
        self.lead.followers_fn = boom
        (self.tasks / "task-h.claimed-core-2.txt").write_text("x")
        self.alive["core-2"] = False
        self.assertEqual(self.lead.reclaim_claimed(),
                         [("task-h.claimed-core-2.txt", "repooled")])


class DeliveredTasksKeepTheirDisposition(WakeGuardBase):
    def test_delivered_claim_still_reads_delivered_after_the_window(self):
        (self.tasks / "task-i.claimed-core-2.txt").write_text("x")
        done = self.state / "cores" / "core-2" / "done"
        done.mkdir(parents=True)
        (done / "task-i.flag").write_text("")
        (self.results / "task-i.txt").write_text("answer")
        self._sleep_whole_host(968)
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.clock += LEAD_STALE_S + 1
        self.assertEqual(self.lead.reclaim_claimed(),
                         [("task-i.claimed-core-2.txt", "delivered")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
