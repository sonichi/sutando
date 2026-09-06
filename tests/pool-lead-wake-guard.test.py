#!/usr/bin/env python3
"""Wake guard: every follower going stale in the same instant is a host gap
(sleep, clock jump), not N deaths, so reclaim defers one stale window.

A 968s clamshell sleep aged every .alive file past the window at once; the lead
read that as four dead cores and repooled a live core's claim, bouncing an owner
task across cores for ~45 minutes. Exercises the production reclaim entrypoints.
Run: python3 tests/pool-lead-wake-guard.test.py   (stdlib only)
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_follower import LEAD_STALE_S  # noqa: E402
from pool_lead import ASSIGN_STUCK_S, PoolLead  # noqa: E402


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


class TheWakeRecordHasOneWriterContract(WakeGuardBase):
    """Overlapping leads are production-supported (TOCTOU pgrep + KeepAlive),
    so the shared record needs collision-safe staging and merge semantics."""

    def _open_deadline(self):
        self.lead._save_wake_evidence(self.clock, self.mono, self.mono + LEAD_STALE_S)
        return self.mono + LEAD_STALE_S

    def test_concurrent_writers_never_publish_a_torn_record(self):
        # NON-DISCRIMINATING on this platform: in-process threads did not
        # reproduce the tear at the parent. Kept as a non-regression guard.
        want = self._open_deadline()
        start = threading.Barrier(4)

        def writer(n):
            start.wait()
            for i in range(40):
                # Variable-length payloads tear a shared temp name; mono stays
                # <= the reader's, since a future mono reads as a reboot.
                self.lead._save_wake_evidence(self.clock + n, self.mono - n,
                                              want if i % 2 else None)
        ts = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        got = self.lead._load_wake_evidence()
        self.assertIsNotNone(got, "concurrent writers published a torn record")
        self.assertEqual(len(got), 3)

    def _second_lead(self):
        """A second PoolLead over the SAME state dir — the overlapping-lead
        case the pgrep/exec TOCTOU in pool-lead-wrapper.sh admits."""
        return PoolLead(self.tasks, self.state,
                        followers_fn=lambda: list(self.pool),
                        alive_fn=lambda i: self.alive.get(i, False),
                        now_fn=lambda: self.clock,
                        mono_fn=lambda: self.mono,
                        results_dir=self.results)

    def test_a_stale_read_modify_write_cannot_erase_a_newer_open_deadline(self):
        """The DISCRIMINATING interleaving: a deadline-free writer loads the
        record, another lead opens grace, then the first one publishes. Unique
        temp names do not order these — only a lock held across load..replace
        does, so this fails on the per-writer-temp parent."""
        other = self._second_lead()
        want = self.mono + LEAD_STALE_S
        loaded = threading.Event()
        real_load = self.lead._load_wake_evidence

        def load_then_yield():
            got = real_load()             # the REAL load, under the real lock
            loaded.set()
            time.sleep(0.25)              # hold the transaction open
            return got
        self.lead._load_wake_evidence = load_then_yield

        opened = []

        def opener():
            loaded.wait(5)
            # production writer, the deadline-carrying half
            opened.append(other._save_wake_evidence(self.clock, self.mono, want)[0])
        t = threading.Thread(target=opener)
        t.start()
        # production writer, the deadline-FREE half that must not clobber
        self.lead._save_wake_evidence(self.clock, self.mono, None)
        t.join(10)
        self.lead._load_wake_evidence = real_load

        self.assertEqual(opened, [True], "opener never published")
        got = self.lead._load_wake_evidence()
        self.assertIsNotNone(got, "record lost entirely")
        self.assertEqual(
            got[2], want,
            "a stale deadline-free write erased a newer open deadline — "
            "the successor loses its grace and repools a live claim")

    def test_a_shorter_deadline_never_shortens_the_open_window(self):
        """Same lost-update shape with BOTH writers carrying a deadline: the
        maximum still-open window is the one that belongs to the wake event."""
        longer = self.mono + LEAD_STALE_S
        self.lead._save_wake_evidence(self.clock, self.mono, longer)
        other = self._second_lead()
        other._save_wake_evidence(self.clock, self.mono, self.mono + 1.0)
        got = self.lead._load_wake_evidence()
        self.assertEqual(got[2], longer, "a shorter sample truncated the window")

    def _seed_a_lead_with_no_window_of_its_own(self):
        """Give the lead an in-process sample with followers ALIVE, so the
        cold-lead fallback cannot fire and there is no skew. Without this the
        lead opens its OWN window and the peer's deadline is never needed —
        which is exactly how the first version of these tests was vacuous."""
        self.alive = {i: True for i in self.pool}
        self.assertFalse(self.lead._host_gap_defers_reclaim(),
                         "seed opened a window — the lead is not neutral")
        self.clock += 2
        self.mono += 2          # awake seconds: both clocks move, no skew

    def test_a_lagging_lead_adopts_the_grace_a_peer_committed(self):
        """keweichen: the lock made the RECORD atomic, but the DECISION was
        still read from the caller's PRE-merge value, so a lead that had just
        merged a peer's open deadline still repooled a live claim. Drives the
        production `reclaim_claimed()` post-liveness re-check."""
        live = self.tasks / "task-live.claimed-core-2.txt"
        live.write_text("x")
        self._seed_a_lead_with_no_window_of_its_own()
        other, opened = self._second_lead(), {}

        def alive(_inst):
            # core-2 is dead, and a PEER opens grace during this very
            # liveness read — the window the re-check exists to catch.
            if not opened:
                self.clock += 2
                self.mono += 2
                du = self.mono + LEAD_STALE_S
                other._reclaim_defer_until = du
                other._save_wake_evidence(self.clock, self.mono, du)
                opened["du"] = du
            return False
        self.lead.alive_fn = alive

        got = self.lead.reclaim_claimed()

        self.assertTrue(opened, "the peer never opened grace — probe never fired")
        self.assertEqual(got, [], "repooled a live claim while a peer's grace was open")
        self.assertTrue(live.exists(), "the live claim was renamed away")

    def test_control_once_the_window_expires_the_same_path_DOES_reclaim(self):
        """Stops the test above from passing by simply never reclaiming."""
        live = self.tasks / "task-live.claimed-core-2.txt"
        live.write_text("x")
        self._seed_a_lead_with_no_window_of_its_own()
        self.alive = {i: False for i in self.pool}
        self.lead.alive_fn = lambda _i: False
        got = self.lead.reclaim_claimed()
        self.assertNotEqual(got, [],
                            "with no peer grace open the path refused to reclaim — "
                            "the deferral is not conditional at all")

    def test_an_older_sample_never_replaces_a_newer_one(self):
        """The loader bounds a deadline by the STORED mono, so publishing an
        older mono beside a newer deadline VOIDS that grace on the next read —
        the record stays writable while the protection silently disappears."""
        other = self._second_lead()
        self.clock += 4
        self.mono += 4
        du = self.mono + LEAD_STALE_S
        other._save_wake_evidence(self.clock, self.mono, du)

        ok, eff = self.lead._save_wake_evidence(self.clock - 4, self.mono - 4, None)

        self.assertTrue(ok, "the lagging write did not publish")
        rec = self.lead._load_wake_evidence()
        self.assertIsNotNone(rec, "record unreadable after the lagging write")
        self.assertEqual(rec[2], du,
                         "an older sample voided the peer's open deadline on read")
        self.assertEqual(eff, du, "the writer did not report the committed deadline")

    def test_an_unpublishable_window_is_refused_and_reported_not_renewed(self):
        """keweichen: `_save_wake_evidence` returning False was discarded at
        both call sites, so a failing publish let each restart re-open the same
        window forever. The bound lives IN the record, so grace it cannot
        record must be refused — and said out loud."""
        (self.tasks / "task-live.claimed-core-2.txt").write_text("x")
        self.lead._save_wake_evidence(self.clock, self.mono, None)
        self._sleep_whole_host(968)      # skew: wall jumps, mono does not

        self.lead._save_wake_evidence = lambda *a, **k: (False, None)  # disk refuses
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            deferred = self.lead._host_gap_defers_reclaim()

        self.assertFalse(deferred,
                         "granted a grace window it could not record, so every "
                         "restart re-opens it and recovery never runs")
        self.assertIn("not published", err.getvalue(),
                      "publication failure was swallowed at the call site")
        self.assertIsNone(getattr(self.lead, "_reclaim_defer_until", None),
                          "an unrecordable window stayed armed in-process")

    def test_a_deadline_free_sample_does_not_erase_an_open_deadline(self):
        want = self._open_deadline()
        self.lead._save_wake_evidence(self.clock, self.mono, None)
        got = self.lead._load_wake_evidence()
        self.assertIsNotNone(got)
        self.assertEqual(got[2], want,
                         "a deadline-free sample erased the open grace window")

    def test_a_torn_record_would_reach_the_reclaim_decision(self):
        # CONTROL for the consequence: a successor seeding from the record must
        # still defer, which is what keeps a live claim from being repooled.
        want = self._open_deadline()
        self.lead._save_wake_evidence(self.clock, self.mono, None)
        successor = self.lead
        for attr in ("_last_reclaim_tick", "_last_reclaim_mono", "_reclaim_defer_until"):
            if hasattr(successor, attr):
                delattr(successor, attr)
        self.assertTrue(successor._host_gap_defers_reclaim(),
                        "successor lost the grace and would repool a live claim")
        self.assertLess(self.mono, want)

    def test_publication_failure_is_observable(self):
        real = Path.write_text

        def boom(self_p, *a, **k):
            raise OSError("disk full")
        Path.write_text = boom
        try:
            ok, _eff = self.lead._save_wake_evidence(self.clock, self.mono, None)
        finally:
            Path.write_text = real
        # `is False`, not assertFalse: the parent returns None, which is falsy
        # and would pass — a check that cannot fail certifies nothing.
        self.assertIs(ok, False, "a failed publication reported success")


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

    def _sleep_at_nth_wall_read(self, n, seconds=968):
        """Wall-only jump on the Nth now() call, owner still ALIVE. The stuck
        sweep skips dead owners, so only a live-owner skew reaches its rename."""
        calls = {"n": 0}
        real = self.lead.now

        def now():
            calls["n"] += 1
            if calls["n"] == n:
                self.clock += seconds       # wall moves, monotonic does NOT
            return real()
        self.lead.now = now
        return calls

    def test_stuck_sweep_declines_after_a_mid_scan_suspension(self):
        f = self.tasks / "task-midscan.assigned-core-2.txt"
        f.write_text("x")
        self.lead.reclaim_stuck_assignments()          # adopt into the ledger
        self.clock += ASSIGN_STUCK_S + 10
        self.mono += ASSIGN_STUCK_S + 10               # awake: both move together
        self.assertTrue(self.alive["core-2"], "owner must stay alive for this path")
        self._sleep_at_nth_wall_read(3)                # the first age read
        self.assertEqual(self.lead.reclaim_stuck_assignments(), [],
                         "a live core's assignment was repooled on a suspension "
                         "the entry guard could not see")
        self.assertEqual(self._names(), ["task-midscan.assigned-core-2.txt"])

    def test_control_stuck_sweep_still_repools_with_no_suspension(self):
        f = self.tasks / "task-stuck.assigned-core-2.txt"
        f.write_text("x")
        self.lead.reclaim_stuck_assignments()
        self.clock += ASSIGN_STUCK_S + 10
        self.mono += ASSIGN_STUCK_S + 10
        self.assertEqual(self.lead.reclaim_stuck_assignments(),
                         ["task-stuck.assigned-core-2.txt"],
                         "control: without a skew the sweep must still repool")
        self.assertEqual(self._names(), ["task-stuck.txt"])

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

        # The ordering under test: sibling back first, claim owner still due.
        self.alive["core-1"] = True
        self.alive["core-2"] = False
        dead, claimed, stuck = self._daemon_order(self._restart_lead())
        self.assertEqual((dead, claimed, stuck), ([], [], []),
                         "a restarted lead repooled a live claim on a sibling-first wake")
        self.assertEqual(self._names(), ["task-restart.claimed-core-2.txt"])

    def test_control_no_predecessor_evidence_keeps_the_old_behaviour(self):
        """Nothing persisted: the all-stale fallback still decides. Asserted
        through behaviour, not the new helper, so it runs unchanged against
        the parent — an equivalence control has to be able to."""
        (self.tasks / "task-restart.claimed-core-2.txt").write_text("x")
        for inst in self.pool:
            self.alive[inst] = False
        lead = self._restart_lead()          # never ticked; no file on disk
        self.assertEqual(lead.reclaim_claimed(), [])
        self.assertEqual(self._names(), ["task-restart.claimed-core-2.txt"])

    def test_a_respawn_inside_the_open_window_keeps_the_grace(self):
        """THE REGRESSION. The deadline used to live only on the PoolLead
        object while the durable sample was overwritten with the post-wake
        pair, so a lead dying inside its own window handed the successor
        evidence showing NO skew -- and a non-None seeded sample also
        excluded it from the cold-lead fallback. Both escapes closed at
        once, and a live core's claim was repooled."""
        (self.tasks / "task-respawn.claimed-core-2.txt").write_text("x")
        self._sleep_whole_host(968)      # wall jumps, monotonic does not
        self.assertEqual(self.lead.reclaim_claimed(), [],
                         "the first lead must open the grace window")
        self.clock += 4
        self.mono += 4                   # 4s later: still inside the window
        self.assertEqual(self._restart_lead().reclaim_claimed(), [],
                         "a successor inside the open window inherits it")
        self.assertEqual(self._names(), ["task-respawn.claimed-core-2.txt"])

    def test_control_a_respawn_after_the_window_still_reclaims(self):
        """The discriminating control: inheritance must expire by TIME, or
        the fix converts one host sleep into an unbounded deferral. Without
        this, a test suite cannot tell the fix from a blanket defer."""
        (self.tasks / "task-respawn.claimed-core-2.txt").write_text("x")
        self._sleep_whole_host(968)
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.clock += LEAD_STALE_S + 10
        self.mono += LEAD_STALE_S + 10   # the window has expired
        self.assertEqual(
            self._restart_lead().reclaim_claimed(),
            [("task-respawn.claimed-core-2.txt", "repooled")],
            "an expired window must not be inherited")

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

    def test_an_unwritable_state_dir_does_not_break_the_sweep(self):
        """Persisting is best-effort: the sample is an optimisation, while
        raising would abort the reclaim sweep that called it."""
        (self.tasks / "task-restart.claimed-core-2.txt").write_text("x")
        (self.state / "pool").write_text("not a directory")
        self.alive["core-2"] = False
        lead = self._restart_lead()
        self.assertEqual(lead.reclaim_claimed(),
                         [("task-restart.claimed-core-2.txt", "repooled")],
                         "a failed evidence write must not stop recovery")
        self.assertIsNone(lead._load_wake_evidence())

    def test_control_a_reboot_discards_the_inherited_sample(self):
        """Monotonic is boot-relative: after a reboot the stored value is
        ahead of ours, which is unusable rather than evidence of a sleep."""
        (self.tasks / "task-restart.claimed-core-2.txt").write_text("x")
        self.lead.reclaim_claimed()
        self.mono -= 400                     # monotonic reset by the reboot
        self.assertIsNone(self._restart_lead()._load_wake_evidence())

if __name__ == "__main__":
    unittest.main(verbosity=2)
