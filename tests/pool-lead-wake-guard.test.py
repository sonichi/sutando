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
        self.mono = 500.0
        self.lead = PoolLead(self.tasks, self.state,
                             followers_fn=lambda: list(self.pool),
                             alive_fn=lambda i: self.alive.get(i, False),
                             now_fn=lambda: self.clock,
                             mono_fn=lambda: self.mono,
                             results_dir=self.results)

    def tearDown(self):
        self.tmp.cleanup()

    def _sleep_whole_host(self, seconds):
        """What a clamshell sleep does: every beat ages together, wall jumps
        and monotonic does NOT — that skew is the evidence under test."""
        for inst in self.pool:
            self.alive[inst] = False
        self.clock += seconds

    def _names(self):
        return sorted(f.name for f in self.tasks.iterdir())

    def _tick_until(self, target, fn):
        """Advance like the live daemon: 2s ticks, collecting reclaim output.
        A single clock leap between calls would read as ANOTHER host sleep."""
        outs = []
        while self.clock < target:
            self.clock += 2
            self.mono += 2          # awake: both clocks advance together
            outs += fn()
        return outs


class SuspensionAfterTheEntryGuard(WakeGuardBase):
    """The entry guard samples the clocks BEFORE scanning, so a suspension
    that begins after it cannot be seen by it — yet the owner liveness read
    that authorizes the rename happens later, and by then the owner reads
    dead. Re-check the skew after that read, or the rename lands on a live
    claim. Both destructive variants must be pinned, not just the claimed one."""

    def _sleep_at_liveness_read(self, owner):
        """alive_fn is what the daemon calls per entry; make the suspension
        begin exactly there, which is the window the entry guard cannot cover."""
        original = self.alive.copy()

        def alive_fn(inst):
            if inst == owner and self.alive.get(inst, False):
                self._sleep_whole_host(968)   # clamshell opens mid-scan
                return False
            return self.alive.get(inst, False)
        self.lead.alive_fn = alive_fn
        return original

    def test_claimed_variant_declines_after_a_mid_scan_suspension(self):
        (self.tasks / "task-race.claimed-core-2.txt").write_text("x")
        self.assertEqual(self.lead.reclaim_claimed(), [])      # baseline tick
        self._sleep_at_liveness_read("core-2")
        self.assertEqual(self.lead.reclaim_claimed(), [],
                         "a live claim was repooled on a suspension the entry guard missed")
        self.assertEqual(self._names(), ["task-race.claimed-core-2.txt"])

    def test_assigned_variant_declines_after_a_mid_scan_suspension(self):
        (self.tasks / "task-race.assigned-core-2.txt").write_text("x")
        self.assertEqual(self.lead.reclaim_dead(), [])         # baseline tick
        self._sleep_at_liveness_read("core-2")
        self.assertEqual(self.lead.reclaim_dead(), [],
                         "an assignment was repooled on a suspension the entry guard missed")
        self.assertEqual(self._names(), ["task-race.assigned-core-2.txt"])

    def test_a_genuinely_dead_owner_with_no_suspension_still_reclaims(self):
        """Control: without the mid-scan clock jump the rename MUST happen,
        so the re-check cannot be passing by simply never reclaiming."""
        (self.tasks / "task-dead.claimed-core-2.txt").write_text("x")
        self.assertEqual(self.lead.reclaim_claimed(), [])      # baseline tick
        self.alive["core-2"] = False
        self.clock += 2
        self.mono += 2
        self.assertEqual(self.lead.reclaim_claimed(),
                         [("task-dead.claimed-core-2.txt", "repooled")])
        self.assertEqual(self._names(), ["task-dead.txt"])


class HostGapDefersReclaim(WakeGuardBase):
    def test_claim_of_live_core_survives_a_host_sleep(self):
        (self.tasks / "task-a.claimed-core-2.txt").write_text("x")
        self.assertEqual(self.lead.reclaim_claimed(), [])  # baseline tick
        self._sleep_whole_host(968)
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.assertEqual(self._names(), ["task-a.claimed-core-2.txt"])

    def test_assignment_survives_a_host_sleep(self):
        (self.tasks / "task-b.assigned-core-1.txt").write_text("x")
        self.assertEqual(self.lead.reclaim_dead(), [])  # baseline tick
        self._sleep_whole_host(968)
        self.assertEqual(self.lead.reclaim_dead(), [])
        self.assertEqual(self._names(), ["task-b.assigned-core-1.txt"])

    def test_stuck_sweep_defers_too(self):
        (self.tasks / "task-c.assigned-core-1.txt").write_text("x")
        self.lead.reclaim_stuck_assignments()  # adopt into the ledger
        self._sleep_whole_host(968)
        self.assertEqual(self.lead.reclaim_stuck_assignments(), [])
        self.assertEqual(self._names(), ["task-c.assigned-core-1.txt"])

    def test_claim_owner_rebeat_within_grace_keeps_claim(self):
        (self.tasks / "task-d.claimed-core-2.txt").write_text("x")
        self._sleep_whole_host(968)
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.alive["core-2"] = True          # beat resumes on wake
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.assertEqual(self._names(), ["task-d.claimed-core-2.txt"])

    def test_sibling_beating_first_does_not_end_the_grace(self):
        # after wake a sibling can beat BEFORE the claim owner; the grace
        # must hold on the lead's tick gap, not on any-follower-alive
        (self.tasks / "task-e.claimed-core-2.txt").write_text("x")
        self.assertEqual(self.lead.reclaim_claimed(), [])  # pre-sleep tick
        self._sleep_whole_host(968)
        self.alive["core-1"] = True          # sibling beats first
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.assertEqual(self.lead.reclaim_dead(), [])
        self.assertEqual(self._names(), ["task-e.claimed-core-2.txt"])

    def test_sibling_first_after_lead_observed_all_stale(self):
        # other ordering: lead observes all-stale, THEN the sibling
        # beats — the open window must not close early
        (self.tasks / "task-f.claimed-core-2.txt").write_text("x")
        self.assertEqual(self.lead.reclaim_claimed(), [])  # pre-sleep tick
        self._sleep_whole_host(968)
        self.assertEqual(self.lead.reclaim_claimed(), [])  # observes all-stale
        self.alive["core-1"] = True          # sibling beats second
        self.clock += 2
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.assertEqual(self._names(), ["task-f.claimed-core-2.txt"])

    def test_grace_expires_by_time_and_still_stale_claimant_repools(self):
        # the window is a grace, not amnesty: a claimant that never
        # re-beats is reclaimed once the window passes, sibling alive or not.
        (self.tasks / "task-g.claimed-core-2.txt").write_text(
            "id: task-g\n")
        self.assertEqual(self.lead.reclaim_claimed(), [])  # pre-sleep tick
        self._sleep_whole_host(968)
        self.alive["core-1"] = True
        self.assertEqual(self.lead.reclaim_claimed(), [])  # in grace
        outs = self._tick_until(968 + 1_000 + LEAD_STALE_S + 4,
                                self.lead.reclaim_claimed)
        self.assertEqual(outs, [("task-g.claimed-core-2.txt", "repooled")])


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
        outs = self._tick_until(self.clock + LEAD_STALE_S + 4,
                                self.lead.reclaim_claimed)  # nothing beating
        self.assertEqual(outs, [("task-f.claimed-core-1.txt", "repooled")])
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
        outs = self._tick_until(self.clock + LEAD_STALE_S + 4,
                                self.lead.reclaim_claimed)
        self.assertEqual(outs, [("task-i.claimed-core-2.txt", "delivered")])


class AwakeSlowDaemonIsNotSleep(unittest.TestCase):
    """Kewei re-review blocker: a large tick gap while AWAKE must not renew
    the grace — sleep evidence is wall-vs-monotonic skew, not gap size."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"; self.tasks.mkdir()
        self.state = root / "state"; self.state.mkdir()
        self.results = root / "results"; self.results.mkdir()
        self.wall = 1000.0
        self.mono = 500.0
        self.alive = {"core-1": True, "core-2": True}
        self.lead = PoolLead(self.tasks, self.state,
                             followers_fn=lambda: ["core-1", "core-2"],
                             alive_fn=lambda i: self.alive.get(i, False),
                             now_fn=lambda: self.wall,
                             mono_fn=lambda: self.mono,
                             results_dir=self.results)

    def tearDown(self):
        self.tmp.cleanup()

    def _claim_of_live_core(self):
        f = self.tasks / "task-w.claimed-core-2.txt"
        f.write_text("id: task-w\n")
        return f

    def _baseline_tick(self, claim):
        """Seed the wall/monotonic pair the NEXT tick is compared against.

        Without this the first call hits the fresh-lead branch, and with a
        sibling alive it repools immediately — every later assertion then
        holds for a claim that is already gone, so the gap-only rule passes
        the suite too.
        """
        self.assertEqual(self.lead.reclaim_claimed(), [],
                         "claimant alive: nothing to reclaim yet")
        self.assertTrue(claim.exists(), "baseline must not consume the claim")

    def test_slow_awake_ticks_never_defer(self):
        # wall and monotonic advance together: no skew, no grace, ever
        claim = self._claim_of_live_core()
        self._baseline_tick(claim)
        self.alive["core-2"] = False          # now it genuinely dies
        self.wall += 300.0; self.mono += 300.0
        self.assertEqual(self.lead.reclaim_claimed(),
                         [("task-w.claimed-core-2.txt", "repooled")],
                         "a 300s AWAKE gap is not sleep: reclaim must proceed")
        self.assertFalse(claim.exists())

    def test_repeated_long_awake_gaps_never_renew_a_grace(self):
        # his second shape: a recurring 60s recovery timeout must not keep
        # re-opening the window ahead of the observation that it expired
        claim = self._claim_of_live_core()
        self._baseline_tick(claim)
        self.alive["core-2"] = False
        for _ in range(5):
            self.wall += 60.0; self.mono += 60.0
            if not claim.exists():
                break
            self.lead.reclaim_claimed()
        self.assertFalse(claim.exists(),
                         "repeated awake 60s gaps must not defer forever")

    def test_sleep_skew_opens_one_expiring_grace(self):
        claim = self._claim_of_live_core()
        self._baseline_tick(claim)
        self.alive["core-2"] = False
        self.wall += 300.0; self.mono += 2.0  # host slept ~298s
        self.assertEqual(self.lead.reclaim_claimed(), [], "grace after sleep")
        self.assertTrue(claim.exists(), "claim survives inside the grace")
        self.wall += LEAD_STALE_S + 1; self.mono += LEAD_STALE_S + 1
        self.assertEqual(self.lead.reclaim_claimed(),
                         [("task-w.claimed-core-2.txt", "repooled")],
                         "grace expired: reclaim resumes")
        self.assertFalse(claim.exists())

    def test_grace_expires_on_monotonic_despite_a_backward_wall_step(self):
        # The host clock is adjustable; a backward correction must not stretch
        # one 90s stale window into hours of withheld recovery.
        claim = self._claim_of_live_core()
        self._baseline_tick(claim)
        self.wall += 3600.0                      # a sleep opens grace
        self.mono += 1.0
        self.alive["core-2"] = False             # owner now reads dead
        self.assertEqual(self.lead.reclaim_claimed(), [], "grace must open")
        self.assertTrue(claim.exists())

        self.mono += 91.0                        # past LEAD_STALE_S
        self.wall -= 3600.0                      # ...and the wall steps BACK
        self.assertEqual([r for r, _ in self.lead.reclaim_claimed()],
                         ["task-w.claimed-core-2.txt"],
                         "a wall correction must not extend the grace window")

    def test_grace_still_holds_inside_the_monotonic_window(self):
        # Control: the test above must not pass merely by never deferring.
        claim = self._claim_of_live_core()
        self._baseline_tick(claim)
        self.wall += 3600.0
        self.mono += 1.0
        self.alive["core-2"] = False
        self.assertEqual(self.lead.reclaim_claimed(), [], "grace must open")
        self.mono += 10.0                        # well inside the window
        self.assertEqual(self.lead.reclaim_claimed(), [], "still deferred")
        self.assertTrue(claim.exists())



class RestartedLeadInheritsWakeEvidence(WakeGuardBase):
    """A lead that restarts across a host sleep has no in-process clock
    sample, so tick 1 must read its predecessor's. Ordering matters: if a
    sibling re-beats before the claim owner does, a liveness-only rule sees
    a live pool and repools a claim whose owner is merely still waking."""

    def _restart_lead(self):
        """Same state dir, new instance — what launchd does on respawn."""
        return PoolLead(self.tasks, self.state,
                        followers_fn=lambda: list(self.pool),
                        alive_fn=lambda i: self.alive.get(i, False),
                        now_fn=lambda: self.clock,
                        mono_fn=lambda: self.mono,
                        results_dir=self.results)

    def _daemon_order(self, lead):
        """scripts/pool-lead-daemon.py:143-148 — reclaim_dead, then
        reclaim_claimed, then reclaim_stuck_assignments."""
        return (lead.reclaim_dead(), lead.reclaim_claimed(),
                lead.reclaim_stuck_assignments())

    def test_sibling_first_wake_does_not_repool_a_live_claim(self):
        (self.tasks / "task-restart.claimed-core-2.txt").write_text("x")
        self.lead.reclaim_claimed()          # pre-sleep tick, persists evidence
        self._sleep_whole_host(968)          # wall jumps, monotonic does not
        self.alive["core-1"] = True          # the sibling re-beats FIRST
        self.alive["core-2"] = False         # the claim owner has not yet
        dead, claimed, stuck = self._daemon_order(self._restart_lead())
        self.assertEqual((dead, claimed, stuck), ([], [], []),
                         "a restarted lead repooled a live claim on a sibling-first wake")
        self.assertEqual(self._names(), ["task-restart.claimed-core-2.txt"])

    def test_control_no_predecessor_evidence_keeps_the_old_behaviour(self):
        """Nothing persisted: the all-stale fallback still decides, so this
        change adds a path rather than replacing one."""
        (self.tasks / "task-restart.claimed-core-2.txt").write_text("x")
        for inst in self.pool:
            self.alive[inst] = False
        lead = self._restart_lead()          # never ticked; no file on disk
        self.assertIsNone(lead._load_wake_evidence())
        self.assertEqual(lead.reclaim_claimed(), [])

    def test_control_a_restart_without_a_sleep_still_reclaims_a_dead_owner(self):
        """The discriminating case: inherited evidence must not become a
        blanket deferral. Lead restarts with NO wall/monotonic skew, so a
        genuinely dead owner is reclaimed on tick 1."""
        (self.tasks / "task-restart.claimed-core-2.txt").write_text("x")
        self.lead.reclaim_claimed()          # persists evidence
        self.clock += 2
        self.mono += 2                       # awake: both advance together
        self.alive["core-2"] = False         # owner genuinely gone
        _, claimed, _ = self._daemon_order(self._restart_lead())
        self.assertEqual(claimed, [("task-restart.claimed-core-2.txt", "repooled")])
        self.assertEqual(self._names(), ["task-restart.txt"])

    def test_control_a_reboot_discards_the_inherited_sample(self):
        """Monotonic is boot-relative: after a reboot the stored value is
        ahead of ours, which is unusable rather than evidence of a sleep."""
        (self.tasks / "task-restart.claimed-core-2.txt").write_text("x")
        self.lead.reclaim_claimed()
        self.mono -= 400                     # monotonic reset by the reboot
        self.assertIsNone(self._restart_lead()._load_wake_evidence())

if __name__ == "__main__":
    unittest.main(verbosity=2)
