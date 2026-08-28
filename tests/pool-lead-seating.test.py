#!/usr/bin/env python3
"""Lead-side profile seating: re-seat off dead cores, and change nothing else.

The point of this slice is what it does NOT do — assignment must be byte-for-byte
unaffected while the seating table is maintained. So every test either exercises
reconcile_seating() or asserts sweep() is untouched by it.

Run: python3 tests/pool-lead-seating.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_lead import PoolLead  # noqa: E402
from pool_profiles import ProfileStore  # noqa: E402

LEAD = "pool-lead"
ROOMS = {"!room-a:ag2.space": {"read": True, "write": "scoped"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        self.tasks.mkdir()
        self.state.mkdir()
        self.live = {"core-1", "core-2", "core-3"}
        self.store = ProfileStore(self.state / "pool" / "profiles.json",
                                  lead_label=LEAD)
        self.lead = PoolLead(
            self.tasks, self.state,
            followers_fn=lambda: ["core-1", "core-2", "core-3"],
            alive_fn=lambda i: i in self.live,
            profiles=self.store)
        self.addCleanup(self.tmp.cleanup)

    def make(self, pid="pro-main"):
        return self.store.create(pid, ROOMS, writer=LEAD)

    def load(self, name, inst=None):
        suffix = f".assigned-{inst}" if inst else ""
        (self.tasks / f"task-{name}{suffix}.txt").write_text(
            "id: x\nsource: slack\nchannel_id: C1\naccess_tier: owner\n"
            "priority: normal\ntask: hi\n")


class SeatingTests(Base):
    def test_an_unseated_profile_gets_a_live_core(self):
        self.make()
        moved = self.lead.reconcile_seating()
        self.assertEqual(len(moved), 1)
        pid, core = moved[0]
        self.assertEqual(pid, "pro-main")
        self.assertIn(core, self.live)
        self.assertEqual(self.store.get("pro-main")["seat"]["epoch"], 1)

    def test_a_seated_profile_on_a_live_core_is_left_alone(self):
        self.make()
        self.lead.reconcile_seating()
        before = self.store.get("pro-main")["seat"]
        self.assertEqual(self.lead.reconcile_seating(), [])
        self.assertEqual(self.store.get("pro-main")["seat"], before)

    def test_a_dead_core_is_reseated_and_the_epoch_advances(self):
        self.make()
        self.lead.reconcile_seating()
        held = self.store.get("pro-main")["seat"]["core_id"]
        epoch = self.store.get("pro-main")["seat"]["epoch"]
        self.live.discard(held)
        moved = self.lead.reconcile_seating()
        seat = self.store.get("pro-main")["seat"]
        self.assertEqual(moved, [("pro-main", seat["core_id"])])
        self.assertNotEqual(seat["core_id"], held)
        self.assertEqual(seat["epoch"], epoch + 1)

    def test_total_core_loss_unseats_rather_than_keeping_a_dead_holder(self):
        self.make()
        self.lead.reconcile_seating()
        self.live.clear()
        moved = self.lead.reconcile_seating()
        self.assertEqual(moved, [("pro-main", None)])
        self.assertIsNone(self.store.get("pro-main")["seat"]["core_id"])

    def test_an_already_unseated_profile_with_no_cores_is_not_rewritten(self):
        self.make()
        self.live.clear()
        self.assertEqual(self.lead.reconcile_seating(), [])
        self.assertEqual(self.store.get("pro-main")["seat"]["epoch"], 0)

    def test_seating_prefers_the_least_loaded_core(self):
        self.make()
        self.load("a", "core-1")
        self.load("b", "core-1")
        self.load("c", "core-2")
        moved = self.lead.reconcile_seating()
        self.assertEqual(moved, [("pro-main", "core-3")])

    def test_several_profiles_are_each_seated(self):
        for pid in ("pro-a", "pro-b"):
            self.store.create(pid, {f"!{pid}:x": {"read": True,
                                                  "write": "scoped"}},
                              writer=LEAD)
        moved = self.lead.reconcile_seating()
        self.assertEqual([p for p, _ in moved], ["pro-a", "pro-b"])


class DegradationTests(Base):
    def test_no_store_bound_is_a_no_op(self):
        lead = PoolLead(self.tasks, self.state,
                        followers_fn=lambda: ["core-1"],
                        alive_fn=lambda i: True)
        self.assertEqual(lead.reconcile_seating(), [])

    def test_an_empty_store_is_a_no_op(self):
        self.assertEqual(self.lead.reconcile_seating(), [])
        self.assertFalse((self.state / "pool" / "profiles.json").exists())

    def test_a_corrupt_store_never_provokes_reseating(self):
        self.make()
        p = self.state / "pool" / "profiles.json"
        p.write_text("{not json")
        self.assertEqual(self.lead.reconcile_seating(), [])
        self.assertEqual(p.read_text(), "{not json")


class NoSchedulingEffectTests(Base):
    def test_assignment_ignores_seating_entirely(self):
        """A profile seated on core-3 must not pull this channel's task there;
        assignment still uses affinity plus least-loaded."""
        self.make()
        self.load("a", "core-1")
        self.load("b", "core-1")
        self.load("c", "core-2")
        self.lead.reconcile_seating()
        self.assertEqual(self.store.get("pro-main")["seat"]["core_id"],
                         "core-3")
        self.load("new")
        assigned = dict(self.lead.sweep())
        # core-3 is the lane core here, so owner traffic must NOT land on it
        self.assertEqual(assigned["task-new.txt"], "core-2")

    def test_sweep_output_is_identical_with_and_without_profiles(self):
        bare = PoolLead(self.tasks, self.state,
                        followers_fn=lambda: ["core-1", "core-2", "core-3"],
                        alive_fn=lambda i: i in self.live)
        self.load("one")
        self.assertEqual(bare.sweep(), [("task-one.txt", "core-1")])
        for f in self.tasks.iterdir():
            f.unlink()
        self.make()
        self.lead.reconcile_seating()
        self.load("one")
        self.assertEqual(self.lead.sweep(), [("task-one.txt", "core-1")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
