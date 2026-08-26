#!/usr/bin/env python3
"""PoolLead (L1): priority-ordered assignment, binding room affinity, least-loaded fallback, dead-follower reclaim, no-follower inertness.

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

from pool_lead import (AFFINITY_BUSY_MAX,  # noqa: E402
                       ASSIGN_STUCK_S, NOCLAIM_COOLDOWN_S, PoolLead)


def _pass_awake(clock, seconds, fn):
    """Daemon-cadence advance: a single clock leap reads as host sleep."""
    outs = []
    end = clock[0] + seconds
    while clock[0] < end:
        clock[0] = min(clock[0] + 20, end)
        outs += fn()
    return outs


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

    def _result(self, stem, where="."):
        d = self.tasks.parent / "results" / where
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}.txt").write_text("answer\n")

    def _flag(self, stem, inst):
        d = self.state / "cores" / inst / "done"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}.flag").write_text("")

    def test_result_without_flag_is_not_reassigned(self):
        """finish_task writes the result BEFORE the flag, so result-without-flag
        is the reachable crash residue and the work is already COMPLETE.
        Gating on both halves swept it back out for re-execution."""
        self._task("task-T1.txt")
        self._result("task-T1")            # crashed after result, before flag
        self.assertEqual(self.lead.sweep(), [])
        self.assertEqual(self._names(), ["task-T1.txt"])

    def test_both_halves_present_is_not_reassigned(self):
        self._task("task-T2.txt")
        self._result("task-T2")
        self._flag("task-T2", "core-a")
        self.assertEqual(self.lead.sweep(), [])

    def test_flag_without_result_IS_assigned(self):
        """The unreachable-by-finish_task case, kept as the negative control:
        no result means no user-visible effect, so the task must still run."""
        self._task("task-T3.txt")
        self._flag("task-T3", "core-a")
        self.assertEqual([n for n, _ in self.lead.sweep()], ["task-T3.txt"])

    def test_consumed_result_dispositions_also_count(self):
        for i, where in enumerate(("archive", "undelivered")):
            stem = f"task-T4{i}"
            self._task(f"{stem}.txt")
            self._result(stem, where)
        self.assertEqual(self.lead.sweep(), [])

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

    def test_idle_gap_does_not_move_the_room(self):
        # binding: a week of silence must not lose the room's context core
        self._task("task-c1.txt", channel="C123")
        first = dict(self.lead.sweep())["task-c1.txt"]
        for f in list(self.tasks.iterdir()):
            f.unlink()  # handler finished everything
        self.clock[0] += 7 * 86400
        self._task("task-c2.txt", channel="C123")
        second = dict(self.lead.sweep())["task-c2.txt"]
        self.assertEqual(second, first)
        row = self.lead._load_affinity()["C123"]
        self.assertEqual(row["ts"], self.clock[0])

    def test_least_loaded_gets_channelless_work(self):
        self.alive["core-c"] = True  # a/b stay in the owner lane (c = routine)
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
    """Crash-mid-claim recovery: delivered = done-flag AND result evidence;
    a flag alone (crash between flag and result write) must repool."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tasks = self.tmp / "tasks"
        self.state = self.tmp / "state"
        self.results = self.tmp / "results"
        self.tasks.mkdir()
        self.results.mkdir()
        (self.state / "cores").mkdir(parents=True)
        self.lead = PoolLead(
            self.tasks, self.state,
            followers_fn=lambda: ["core-1"],
            alive_fn=lambda inst: inst == "core-1",
            results_dir=self.results,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _claimed(self, tid, inst):
        f = self.tasks / f"task-{tid}.claimed-{inst}.txt"
        f.write_text(f"id: task-{tid}\n")
        return f

    def _flag(self, tid, inst):
        done = self.state / "cores" / inst / "done"
        done.mkdir(parents=True, exist_ok=True)
        (done / f"task-{tid}.flag").write_text("")

    def test_dead_claimer_without_done_flag_repools(self):
        self._claimed("x1", "core-9")
        out = self.lead.reclaim_claimed()
        self.assertEqual(out, [("task-x1.claimed-core-9.txt", "repooled")])
        self.assertTrue((self.tasks / "task-x1.txt").exists())

    def test_dead_claimer_with_flag_and_result_restores_for_delivery_only(self):
        self._claimed("x2", "core-9")
        self._flag("x2", "core-9")
        (self.results / "task-x2.txt").write_text("the reply")
        out = self.lead.reclaim_claimed()
        self.assertEqual(out, [("task-x2.claimed-core-9.txt", "delivered")])
        self.assertTrue((self.tasks / "task-x2.txt").exists())
        self.assertEqual(self.lead.sweep(), [])  # never reassigned

    def test_flag_without_result_repools_and_reassigns(self):
        # the silent-loss edge: crash after flag, before result write
        self._claimed("x5", "core-9")
        self._flag("x5", "core-9")
        out = self.lead.reclaim_claimed()
        self.assertEqual(out, [("task-x5.claimed-core-9.txt", "repooled")])
        self.assertEqual(self.lead.sweep(), [("task-x5.txt", "core-1")])

    def test_bridge_archived_result_still_counts_as_delivered(self):
        self._claimed("x6", "core-9")
        self._flag("x6", "core-9")
        (self.results / "archive").mkdir()
        (self.results / "archive" / "task-x6.txt").write_text("consumed")
        out = self.lead.reclaim_claimed()
        self.assertEqual(out, [("task-x6.claimed-core-9.txt", "delivered")])
        self.assertEqual(self.lead.sweep(), [])

    def test_live_claimer_untouched(self):
        f = self._claimed("x3", "core-1")
        self.assertEqual(self.lead.reclaim_claimed(), [])
        self.assertTrue(f.exists())

    def test_repooled_task_is_reassignable(self):
        self._claimed("x4", "core-9")
        self.lead.reclaim_claimed()
        assigned = self.lead.sweep()
        self.assertEqual(assigned, [("task-x4.txt", "core-1")])




class AffinityBusyYieldTest(unittest.TestCase):
    """Binding affinity (owner 2026-08-26): a room's home core keeps its
    work while alive — load never moves a room, only death or an
    unclaimed-reclaim does. Context continuity outranks latency."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tasks = self.tmp / "tasks"
        self.state = self.tmp / "state"
        self.tasks.mkdir(); (self.state / "pool").mkdir(parents=True)
        self.lead = PoolLead(
            self.tasks, self.state,
            followers_fn=lambda: ["core-1", "core-2", "core-3"],
            alive_fn=lambda inst: True,
        )
        import json
        (self.state / "pool" / "affinity.json").write_text(
            json.dumps({"chan-A": {"instance": "core-2", "ts": self.lead.now()}}))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _backlog(self, inst, n):
        for i in range(n):
            (self.tasks / f"task-b{i}.claimed-{inst}.txt").write_text("x")

    def _new_task(self):
        f = self.tasks / "task-fresh.txt"
        f.write_text("id: task-fresh\nchannel_id: chan-A\n")
        return f

    def test_busy_home_core_keeps_the_room(self):
        self._backlog("core-2", 5)  # far past any depth threshold
        self._new_task()
        out = dict(self.lead.sweep())
        self.assertEqual(out.get("task-fresh.txt"), "core-2")

    def test_handler_below_threshold_keeps_channel(self):
        self._backlog("core-2", 2)
        self._new_task()
        out = dict(self.lead.sweep())
        self.assertEqual(out.get("task-fresh.txt"), "core-2")

    def test_routine_assignment_never_rebinds_a_room(self):
        # owner report 2026-08-26: a routine task tagged with a room stamped
        # the lane core over the owner's binding, making the steal sticky.
        (self.tasks / "task-r9.txt").write_text(
            "id: task-r9\nchannel_id: chan-A\npriority: low\ntask: t\n")
        self.lead.sweep()
        row = self.lead._load_affinity()["chan-A"]
        self.assertEqual(row["instance"], "core-2", "binding must survive")

    def test_busy_core_keeps_unclaimed_assignments_and_its_rooms(self):
        # busy != wedged: a mid-task core claims later; repooling moved
        # bound rooms off their context core
        (self.tasks / "task-w1.claimed-core-2.txt").write_text("x")
        stuck = self.tasks / "task-w2.assigned-core-2.txt"
        stuck.write_text("id: task-w2\nchannel_id: chan-A\n")
        self.lead._save_assign_ledger({stuck.name: self.lead.now() - 301.0})
        out = self.lead.reclaim_stuck_assignments(max_age_s=300)
        self.assertEqual(out, [])
        self.assertTrue(stuck.exists(), "assignment must stay put")
        self.assertIn("chan-A", self.lead._load_affinity())

    def test_wedged_busy_core_yields_at_the_deferral_cap(self):
        # a claimed file is not proof of progress: past BUSY_DEFER_MAX_S the
        # assignment repools even though the core still holds a claim
        (self.tasks / "task-w3.claimed-core-2.txt").write_text("x")
        stuck = self.tasks / "task-w4.assigned-core-2.txt"
        stuck.write_text("id: task-w4\nchannel_id: chan-A\n")
        import os as _os
        _os.utime(stuck, (self.lead.now() - 1801.0,) * 2)
        self.lead._save_assign_ledger({stuck.name: self.lead.now() - 1801.0})
        out = self.lead.reclaim_stuck_assignments(max_age_s=300)
        self.assertEqual(len(out), 1)
        self.assertFalse(stuck.exists(), "past the cap the assignment repools")

    def test_post_busy_grace_lets_the_core_claim_before_repool(self):
        # a core that was busy through the whole window gets a fresh short
        # claim grace when the busy spell ends, instead of an instant repool
        busy = self.tasks / "task-g1.claimed-core-2.txt"
        busy.write_text("x")
        stuck = self.tasks / "task-g2.assigned-core-2.txt"
        stuck.write_text("id: task-g2\nchannel_id: chan-A\n")
        self.lead._save_assign_ledger({stuck.name: self.lead.now() - 301.0})
        self.assertEqual(self.lead.reclaim_stuck_assignments(max_age_s=300), [])
        ledger = self.lead._load_assign_ledger()
        age = self.lead.now() - ledger[stuck.name]
        self.assertLess(age, 300.0, "busy sweep must leave a claim grace")
        busy.unlink()  # busy spell ends
        self.assertEqual(self.lead.reclaim_stuck_assignments(max_age_s=300), [],
                         "inside the grace window nothing repools")
        self.lead._save_assign_ledger({stuck.name: self.lead.now() - 361.0})
        out = self.lead.reclaim_stuck_assignments(max_age_s=300)
        self.assertEqual(len(out), 1, "grace expired unclaimed: repool")

    def test_stale_queued_task_still_gets_the_busy_cap_from_assignment(self):
        # rename preserves arrival mtime; assignment must stamp its own time
        # or a task queued > cap is repooled the moment the claim window ends
        import os as _os
        (self.tasks / "task-s1.claimed-core-2.txt").write_text("x")
        old = self.tasks / "task-s2.txt"
        old.write_text("id: task-s2\nsource: slack\nchannel_id: chan-A\n"
                       "access_tier: owner\npriority: normal\ntask: hi\n")
        _os.utime(old, (self.lead.now() - 7200.0,) * 2)  # queued two hours
        self.lead.sweep()
        stuck = self.tasks / "task-s2.assigned-core-2.txt"
        self.assertTrue(stuck.exists())
        self.lead._save_assign_ledger({stuck.name: self.lead.now() - 301.0})
        out = self.lead.reclaim_stuck_assignments(max_age_s=300)
        self.assertEqual(out, [], "busy cap must measure from assignment")

    def test_unclaimed_reclaim_releases_the_room_binding(self):
        # home core heartbeats but never claims: reclaim must drop the row
        # so the re-pick moves the room to a core that answers.
        stuck = self.tasks / "task-stuck.assigned-core-2.txt"
        stuck.write_text("id: task-stuck\nchannel_id: chan-A\n")
        self.lead._save_assign_ledger({stuck.name: 0.0})
        self.lead.reclaim_stuck_assignments(max_age_s=1)
        self.assertNotIn("chan-A", self.lead._load_affinity())
        out = dict(self.lead.sweep())
        self.assertNotEqual(out.get("task-stuck.txt"), "core-2")
        self.assertEqual(
            self.lead._load_affinity()["chan-A"]["instance"],
            out["task-stuck.txt"])  # new home re-stamped


class RoutineLaneEscape(unittest.TestCase):
    """A wedged lane core must not absorb every routine task. Its heartbeat
    stays fresh throughout, so failing-to-claim is the only usable signal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"; self.tasks.mkdir()
        self.state = root / "state"; self.state.mkdir()
        self.alive = {"core-1": True, "core-2": True, "core-3": True}
        self.clock = [1000.0]
        self.lead = PoolLead(
            self.tasks, self.state,
            followers_fn=lambda: list(self.alive),
            alive_fn=lambda i: self.alive.get(i, False),
            now_fn=lambda: self.clock[0])
        # highest-numbered follower is the routine lane
        self.lane = max(self.alive, key=lambda f: (len(f), f))

    def tearDown(self):
        self.tmp.cleanup()

    def _routine(self, name):
        (self.tasks / name).write_text(f"id: {name[:-4]}\npriority: low\ntask: t\n")

    def test_lane_core_that_never_claims_stops_receiving_routine_work(self):
        """The live loop: assign -> no claim -> repool -> assign again. The
        lane core heartbeats the whole time, so only the repool distinguishes
        it from a busy follower."""
        self._routine("task-r1.txt")
        first = dict(self.lead.sweep())["task-r1.txt"]
        self.assertEqual(first, self.lane, "routine should start on the lane core")

        # it never claims. The lead reclaims every pass: the first sighting
        # only adopts the assignment into the ledger, the next one repools it.
        self.assertEqual(self.lead.reclaim_stuck_assignments(), [])
        outs = _pass_awake(self.clock, ASSIGN_STUCK_S + 1,
                           self.lead.reclaim_stuck_assignments)
        self.assertEqual(outs, [f"task-r1.assigned-{self.lane}.txt"])
        self.assertTrue(self.alive[self.lane], "lane core still heartbeats")

        second = dict(self.lead.sweep())["task-r1.txt"]
        self.assertNotEqual(second, self.lane,
                            "repooled routine work went straight back to the "
                            "follower that just failed to claim it")
        self.assertIn(second, self.alive)

    def test_a_busy_lane_core_keeps_its_lane(self):
        """Load alone must not evict the lane: a follower working through a
        backlog is not the same as one wedged at its input layer."""
        (self.tasks / f"task-busy.claimed-{self.lane}.txt").write_text("x")
        self._routine("task-r2.txt")
        self.assertEqual(dict(self.lead.sweep())["task-r2.txt"], self.lane)

    def test_cooldown_expires_and_the_lane_is_restored(self):
        self._routine("task-r3.txt")
        self.lead.sweep()
        self.lead.reclaim_stuck_assignments()          # adopt
        _pass_awake(self.clock, ASSIGN_STUCK_S + 1,
                    self.lead.reclaim_stuck_assignments)  # repool + mark
        self.assertFalse(self.lead._claiming(self.lane))
        self.clock[0] += NOCLAIM_COOLDOWN_S
        self.assertTrue(self.lead._claiming(self.lane),
                        "cooldown must expire, or a recovered core is exiled")
        for f in list(self.tasks.iterdir()):
            f.unlink()
        self._routine("task-r4.txt")
        self.assertEqual(dict(self.lead.sweep())["task-r4.txt"], self.lane)

    def test_owner_lane_also_skips_a_core_that_failed_to_claim(self):
        """The lane pin was only one branch. A repool DROPS the follower's
        load, so least-loaded actively prefers the core that just failed."""
        owner = [f for f in self.alive if f != self.lane]
        victim = min(owner)                       # least-loaded picks this one
        (self.tasks / "task-o1.txt").write_text("id: task-o1\ntask: t\n")
        self.assertEqual(dict(self.lead.sweep())["task-o1.txt"], victim)

        self.lead.reclaim_stuck_assignments()     # adopt
        outs = _pass_awake(self.clock, ASSIGN_STUCK_S + 1,
                           self.lead.reclaim_stuck_assignments)
        self.assertEqual(outs, [f"task-o1.assigned-{victim}.txt"])
        again = dict(self.lead.sweep())["task-o1.txt"]
        self.assertNotEqual(again, victim,
                            "owner-lane work returned to the core that just "
                            "failed to claim it")
        self.assertNotEqual(again, self.lane, "and must not fall to the lane")

    def test_every_follower_in_cooldown_still_assigns(self):
        """Narrowing to claiming-only must never empty the candidate set —
        a pool where everyone is in cooldown must still place work."""
        for f in self.alive:
            self.lead._mark_noclaim(f)
        (self.tasks / "task-o2.txt").write_text("id: task-o2\ntask: t\n")
        out = dict(self.lead.sweep())
        self.assertIn(out.get("task-o2.txt"), self.alive)

    def test_saturated_lane_core_spills_routine_to_another_follower(self):
        for i in range(AFFINITY_BUSY_MAX):
            (self.tasks / f"task-b{i}.claimed-{self.lane}.txt").write_text("x")
        self._routine("task-r5.txt")
        got = dict(self.lead.sweep())["task-r5.txt"]
        self.assertNotEqual(got, self.lane,
                            "routine pinned to a lane core already at "
                            "AFFINITY_BUSY_MAX outstanding items")



if __name__ == "__main__":
    unittest.main(verbosity=2)
