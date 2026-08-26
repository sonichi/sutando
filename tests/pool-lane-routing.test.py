#!/usr/bin/env python3
"""Soft lanes (L7): routine work pins to the highest core, owner work stays
off it except as saturated overflow, and lane detection fails open to owner.

Exercises the production sweep() path — real task files, real renames.
Run: python3 tests/pool-lane-routing.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_lead import PoolLead, _read_channel, _read_lane  # noqa: E402
from pool_notify import read_routing  # noqa: E402


def owner_task(source="slack", channel="C1"):
    return (f"id: x\nsource: {source}\nchannel_id: {channel}\n"
            f"access_tier: owner\npriority: normal\ntask: hi\n")


def routine_task(**hdr):
    base = {"access_tier": "owner", "priority": "low"}
    base.update(hdr)
    lines = "".join(f"{k}: {v}\n" for k, v in base.items())
    return f"id: x\nsource: cron\n{lines}task: chore\n"


class LaneBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        self.tasks.mkdir()
        self.state.mkdir()
        self.pool = ["core-1", "core-2"]
        self.lead = PoolLead(self.tasks, self.state,
                             followers_fn=lambda: list(self.pool),
                             alive_fn=lambda i: True,
                             now_fn=lambda: 1_000.0)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, body):
        (self.tasks / name).write_text(body)

    def _occupy(self, inst, n):
        for i in range(n):
            (self.tasks / f"task-busy{i}.claimed-{inst}.txt").write_text("x")

    def _assigned_to(self, stem):
        hits = [f.name for f in self.tasks.iterdir() if f.name.startswith(stem + ".assigned-")]
        return hits[0].split(".assigned-")[1][:-len(".txt")] if hits else None


class LaneRoutingTests(LaneBase):
    def test_routine_goes_to_highest_core(self):
        self._write("task-r1.txt", routine_task())
        self.lead.sweep()
        self.assertEqual(self._assigned_to("task-r1"), "core-2")

    def test_non_owner_tier_is_routine(self):
        self._write("task-r2.txt", routine_task(access_tier="team", priority="normal"))
        self.lead.sweep()
        self.assertEqual(self._assigned_to("task-r2"), "core-2")

    def test_self_reflective_is_routine(self):
        self._write("task-r3.txt",
                    routine_task(priority="normal", interaction_type="self_reflective"))
        self.lead.sweep()
        self.assertEqual(self._assigned_to("task-r3"), "core-2")

    def test_owner_stays_off_lane_core_even_when_it_is_idler(self):
        self._occupy("core-1", 1)  # lane core idle, core-1 has load
        self._write("task-o1.txt", owner_task())
        self.lead.sweep()
        self.assertEqual(self._assigned_to("task-o1"), "core-1")

    def test_owner_overflows_to_idle_lane_core_when_saturated(self):
        self._occupy("core-1", 3)  # AFFINITY_BUSY_MAX
        self._write("task-o2.txt", owner_task(channel="C9"))
        self.lead.sweep()
        self.assertEqual(self._assigned_to("task-o2"), "core-2")

    def test_no_overflow_when_lane_core_is_busy_too(self):
        self._occupy("core-1", 3)
        self._occupy("core-2", 1)
        self._write("task-o3.txt", owner_task(channel="C9"))
        self.lead.sweep()
        self.assertEqual(self._assigned_to("task-o3"), "core-1")

    def test_single_core_pool_has_no_lanes(self):
        self.pool = ["core-1"]
        self._write("task-r4.txt", routine_task())
        self._write("task-o4.txt", owner_task())
        self.lead.sweep()
        self.assertEqual(self._assigned_to("task-r4"), "core-1")
        self.assertEqual(self._assigned_to("task-o4"), "core-1")

    def test_explicit_binding_beats_lane_defaults(self):
        # binding rows are always deliberate now (sweep cannot stamp the
        # lane core) — honor them on ANY alive seat, incl. lane core/codex
        (self.state / "pool").mkdir(parents=True)
        (self.state / "pool" / "affinity.json").write_text(
            '{"C1": {"instance": "core-2", "ts": 999.0}}')
        self._write("task-o5.txt", owner_task(channel="C1"))
        self.lead.sweep()
        self.assertEqual(self._assigned_to("task-o5"), "core-2")

    def test_numeric_core_ordering(self):
        self.pool = ["core-2", "core-10"]
        self._write("task-r5.txt", routine_task())
        self.lead.sweep()
        self.assertEqual(self._assigned_to("task-r5"), "core-10")


class LaneDetectionTests(LaneBase):
    def test_missing_headers_fail_open_to_owner(self):
        p = self.tasks / "task-x.txt"
        p.write_text("id: task-x\ntask: bare voice task\n")
        self.assertEqual(_read_lane(p), "owner")

    def test_unreadable_fails_open_to_owner(self):
        self.assertEqual(_read_lane(self.tasks / "absent.txt"), "owner")


class RuntimeAwareLaneTests(LaneBase):
    """The routine lane skips codex: it has no watcher and sweeps on a timer,
    so maintenance parked there waits for a poll."""

    def _lead(self, runtimes):
        self.pool = sorted(runtimes)
        return PoolLead(self.tasks, self.state,
                        followers_fn=lambda: list(self.pool),
                        alive_fn=lambda i: True, now_fn=lambda: 1_000.0,
                        runtime_fn=lambda i: runtimes[i])

    def test_routine_skips_codex_for_the_highest_claude_core(self):
        lead = self._lead({"core-1": "claude", "core-2": "claude",
                           "core-3": "codex"})
        self._write("task-r1.txt", routine_task())
        lead.sweep()
        self.assertEqual(self._assigned_to("task-r1"), "core-2")

    def test_owner_work_queues_on_claude_over_idle_codex(self):
        # owner 2026-08-26: a codex seat is turnaround-slow, so owner work
        # waits behind a lightly-loaded claude seat rather than landing there.
        lead = self._lead({"core-1": "claude", "core-2": "claude",
                           "core-3": "codex"})
        self._occupy("core-1", 1)
        self._write("task-o1.txt", owner_task())
        lead.sweep()
        self.assertEqual(self._assigned_to("task-o1"), "core-1")

    def test_all_codex_pool_still_has_a_lane(self):
        lead = self._lead({"core-1": "codex", "core-2": "codex"})
        self._write("task-r2.txt", routine_task())
        lead.sweep()
        self.assertEqual(self._assigned_to("task-r2"), "core-2")

    def test_explicit_binding_to_codex_is_honored(self):
        lead = self._lead({"core-1": "claude", "core-2": "claude",
                           "core-3": "codex"})
        (self.state / "pool").mkdir(parents=True, exist_ok=True)
        (self.state / "pool" / "affinity.json").write_text(
            '{"C2": {"instance": "core-3", "ts": 999.0}}')
        self._write("task-o6.txt", owner_task(channel="C2"))
        lead.sweep()
        self.assertEqual(self._assigned_to("task-o6"), "core-3")

    def test_single_claude_with_codex_gets_owner_work(self):
        # the sole claude doubles as lane core but must stay owner-eligible
        lead = self._lead({"core-1": "claude", "core-4": "codex"})
        self._write("task-o7.txt", owner_task())
        lead.sweep()
        self.assertEqual(self._assigned_to("task-o7"), "core-1")

    def test_single_claude_with_codex_gets_routine_work(self):
        lead = self._lead({"core-1": "claude", "core-4": "codex"})
        self._write("task-r6.txt", routine_task())
        lead.sweep()
        self.assertEqual(self._assigned_to("task-r6"), "core-1")

    def test_no_runtime_fn_keeps_the_pre_runtime_behaviour(self):
        self._write("task-r3.txt", routine_task())
        self.lead.sweep()
        self.assertEqual(self._assigned_to("task-r3"), "core-2")


class HeaderPastTheOldCap(LaneBase):
    """Header fields are read from the whole file. The former 2048-byte slice
    made a well-formed header indistinguishable from an absent one."""

    # Offsets measured by a reviewer over 600 archived task files: the fields
    # sat past 2048 in ~35% of them, at 2054-10714.
    NEAR, FAR = 2054, 10714

    def _task(self, name, header, at):
        pad = "x" * max(0, at - len("id: t\n"))
        p = self.tasks / name
        p.write_text(f"id: t\n{pad}\n{header}\ntask: body\n")
        self.assertGreater(p.read_text().index(header.split(":")[0]), 2048,
                           "fixture must place the field past the old cap")
        return p

    def test_access_tier_just_past_the_cap_is_seen(self):
        p = self._task("task-near.txt", "access_tier: team", self.NEAR)
        self.assertEqual(_read_lane(p), "routine")

    def test_access_tier_far_past_the_cap_is_seen(self):
        p = self._task("task-far.txt", "access_tier: team", self.FAR)
        self.assertEqual(_read_lane(p), "routine")

    def test_control_same_header_early_is_seen(self):
        """Without this the pair above could pass for the wrong reason — the
        function was always correct on headers it could see."""
        p = self.tasks / "task-early.txt"
        p.write_text("id: t\naccess_tier: team\ntask: body\n")
        self.assertEqual(_read_lane(p), "routine")

    def test_owner_header_past_the_cap_still_reads_owner(self):
        p = self._task("task-owner.txt", "access_tier: owner", self.NEAR)
        self.assertEqual(_read_lane(p), "owner")

    def test_channel_past_the_cap_is_seen(self):
        p = self._task("task-chan.txt", "channel_id: C4242", self.NEAR)
        self.assertEqual(_read_channel(p), "C4242")

    def test_notify_routing_past_the_cap_is_seen(self):
        p = self.tasks / "task-route.txt"
        pad = "x" * self.NEAR
        p.write_text(f"id: t\n{pad}\nsource: discord\nchannel_id: C99\ntask: b\n")
        self.assertEqual(read_routing(p), ("discord", "C99"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
