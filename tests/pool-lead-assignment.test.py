#!/usr/bin/env python3
"""PoolLead (L1): priority-ordered assignment, sticky affinity with idle
rebalance, least-loaded fallback, dead-follower reclaim, no-follower inertness.

Run: python3 tests/pool-lead-assignment.test.py   (stdlib only)
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        # One tick with the pool proven alive first: a cold lead cannot tell a
        # dead follower from one that has not re-beaten, so it defers.
        self.assertEqual(self.lead.reclaim_dead(), [])
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
    """A backlogged affinity handler yields to the least-loaded follower."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tasks = self.tmp / "tasks"
        self.state = self.tmp / "state"
        self.tasks.mkdir(); (self.state / "pool").mkdir(parents=True)
        self.lead = PoolLead(
            self.tasks, self.state,
            followers_fn=lambda: ["core-1", "core-2"],
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

    def test_busy_handler_yields_to_idle_follower(self):
        self._backlog("core-2", 3)  # at AFFINITY_BUSY_MAX
        self._new_task()
        out = dict(self.lead.sweep())
        self.assertEqual(out.get("task-fresh.txt"), "core-1")

    def test_handler_below_threshold_keeps_channel(self):
        self._backlog("core-2", 2)  # under AFFINITY_BUSY_MAX
        self._new_task()
        out = dict(self.lead.sweep())
        self.assertEqual(out.get("task-fresh.txt"), "core-2")


class LeadDegradesOnFilesystemTroubleTests(unittest.TestCase):
    """The lead runs every few seconds against a directory other processes are
    renaming under it. Every scan and every rename must degrade, not raise: an
    exception here stops assignment for the whole pool, not one task."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        self.tasks.mkdir()
        self.state.mkdir()
        self.clock = [1000.0]
        self.lead = PoolLead(
            self.tasks, self.state,
            followers_fn=lambda: ["core-a"],
            alive_fn=lambda i: True,
            now_fn=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name):
        (self.tasks / name).write_text(f"id: {name[:-4]}\n")

    def test_an_unreadable_tasks_dir_yields_empty_results_not_an_exception(self):
        self._write("task-a.txt")
        self.tasks.chmod(0o000)
        try:
            self.assertEqual(self.lead.sweep(), [])
            self.assertEqual(self.lead.reclaim_dead(), [])
            self.assertEqual(self.lead.reclaim_claimed(), [])
        finally:
            self.tasks.chmod(0o700)

    def test_an_unreadable_task_body_is_treated_as_channel_less(self):
        # _channel_of reads the head of the file; an unreadable one must fall
        # back to no-affinity rather than aborting the sweep.
        self._write("task-a.txt")
        (self.tasks / "task-a.txt").chmod(0o000)
        # no chmod back: sweep renames the file, so the original path is gone.
        # Removal depends on the directory's mode, which is untouched.
        self.assertEqual(dict(self.lead.sweep()).get("task-a.txt"), "core-a")

    def test_a_rename_lost_to_another_writer_skips_only_that_task(self):
        self._write("task-a.txt")
        self._write("task-b.txt")
        real, seen = os.rename, []

        def flaky(src, dst):
            if not seen:
                seen.append(src)
                raise OSError("another writer got there first")
            return real(src, dst)

        with mock.patch("pool_lead.os.rename", side_effect=flaky):
            out = dict(self.lead.sweep())
        self.assertEqual(len(out), 1, "one lost rename ended the whole sweep")

    # pool_lead.py:142-143 (stat racing the rename) is deliberately NOT covered.
    # sort_tasks_by_priority stats the same file first and unguarded, so any
    # patch broad enough to reach the lead's guard raises there instead.


if __name__ == "__main__":
    unittest.main(verbosity=2)
