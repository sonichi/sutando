#!/usr/bin/env python3
"""Explicit room pins: a pinned room routes to its worker, survives wedge
release and auto-rebind, loans out (pin retained) when the home is dead or
non-claiming, and unpin restores auto re-homing. CLI wiring included.

Run: python3 tests/pool-lead-pin.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_lead import PoolLead  # noqa: E402


class PoolLeadPinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        self.tasks.mkdir()
        self.state.mkdir()
        self.alive = {"worker-a": True, "worker-b": True}
        self.clock = [1000.0]
        self.lead = PoolLead(
            self.tasks, self.state,
            followers_fn=lambda: list(self.alive),
            alive_fn=lambda i: self.alive.get(i, False),
            now_fn=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, name, channel=None):
        lines = [f"id: {name[:-4]}"]
        if channel:
            lines.append(f"channel_id: {channel}")
        (self.tasks / name).write_text("\n".join(lines) + "\ntask: t\n")

    def _disk_table(self):
        return json.loads((self.state / "pool" / "affinity.json").read_text())

    def test_pinned_room_routes_to_its_worker(self):
        # worker-a would win the least-loaded tie (lexicographic); the pin
        # must send the room's task to worker-b instead.
        self.lead.pin_room("chan-P", "worker-b")
        self._task("task-p1.txt", channel="chan-P")
        self.assertEqual(self.lead.sweep(), [("task-p1.txt", "worker-b")])

    def test_dead_pinned_worker_loans_task_but_keeps_pin(self):
        self.lead.pin_room("chan-P", "worker-b")
        self.alive["worker-b"] = False
        self._task("task-p2.txt", channel="chan-P")
        self.assertEqual(self.lead.sweep(), [("task-p2.txt", "worker-a")])
        row = self._disk_table()["chan-P"]
        self.assertEqual(row["instance"], "worker-b",
                         "auto rebind must not steal a pinned room")
        self.assertTrue(row.get("pinned"))

    def test_recovered_pinned_worker_gets_its_room_back(self):
        self.lead.pin_room("chan-P", "worker-b")
        self.alive["worker-b"] = False
        self._task("task-p3.txt", channel="chan-P")
        self.lead.sweep()
        self.alive["worker-b"] = True
        self._task("task-p4.txt", channel="chan-P")
        picks = dict(self.lead.sweep())
        self.assertEqual(picks["task-p4.txt"], "worker-b")

    def test_nonclaiming_pinned_worker_is_loaned_around(self):
        # a pinned wedge must not starve the room: repool marks the home
        # non-claiming, and the next pick loans to a claiming worker
        self.lead.pin_room("chan-P", "worker-b")
        self.lead._mark_noclaim("worker-b")
        self._task("task-p5.txt", channel="chan-P")
        self.assertEqual(self.lead.sweep(), [("task-p5.txt", "worker-a")])
        self.assertTrue(self._disk_table()["chan-P"].get("pinned"))

    def test_wedge_release_spares_pinned_row(self):
        self.lead.pin_room("chan-P", "worker-b")
        stuck = self.tasks / "task-p6.assigned-worker-b.txt"
        stuck.write_text("id: task-p6\nchannel_id: chan-P\n")
        self.lead._save_assign_ledger({stuck.name: self.lead.now() - 301.0})
        out = self.lead.reclaim_stuck_assignments(max_age_s=300)
        self.assertEqual(out, [stuck.name], "assignment itself must repool")
        self.assertEqual(self._disk_table()["chan-P"]["instance"], "worker-b",
                         "the pin outlives the wedge release")

    def test_wedge_release_still_drops_unpinned_row(self):
        # negative control: shipped auto behavior is unchanged
        self.lead._save_affinity(
            {"chan-A": {"instance": "worker-b", "ts": self.lead.now()}})
        stuck = self.tasks / "task-p7.assigned-worker-b.txt"
        stuck.write_text("id: task-p7\nchannel_id: chan-A\n")
        self.lead._save_assign_ledger({stuck.name: self.lead.now() - 301.0})
        self.lead.reclaim_stuck_assignments(max_age_s=300)
        self.assertNotIn("chan-A", self._disk_table())

    def test_unpin_keeps_binding_but_restores_auto_rehoming(self):
        self.lead.pin_room("chan-P", "worker-b")
        self.assertTrue(self.lead.unpin_room("chan-P"))
        row = self._disk_table()["chan-P"]
        self.assertEqual(row["instance"], "worker-b")
        self.assertNotIn("pinned", row)
        self.alive["worker-b"] = False
        self._task("task-p8.txt", channel="chan-P")
        self.lead.sweep()
        self.assertEqual(self._disk_table()["chan-P"]["instance"], "worker-a",
                         "without the pin, death re-homes the room")

    def test_unpin_without_pin_reports_false(self):
        self.assertFalse(self.lead.unpin_room("chan-none"))
        self.lead._save_affinity(
            {"chan-A": {"instance": "worker-a", "ts": 1.0}})
        self.assertFalse(self.lead.unpin_room("chan-A"))

    def test_sweep_save_merges_around_a_concurrent_pin(self):
        # a CLI pin lands after the sweep's initial table load but before
        # its save; the locked re-read at save time must keep the pin
        self._task("task-p9.txt", channel="chan-Q")
        real_load = self.lead._load_affinity
        real_save = self.lead._save_affinity
        calls = {"n": 0}

        def racing_load():
            calls["n"] += 1
            if calls["n"] == 2:  # the merge's locked re-read
                real_save({"chan-Q": {"instance": "worker-b", "ts": 9.0,
                                      "pinned": True}})
            return real_load()

        self.lead._load_affinity = racing_load
        self.lead.sweep()
        self.lead._load_affinity = real_load
        row = self._disk_table()["chan-Q"]
        self.assertEqual(row["instance"], "worker-b")
        self.assertTrue(row.get("pinned"))


class PoolBindCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()
        (self.ws / "state").mkdir()
        spec = importlib.util.spec_from_file_location(
            "pool_bind", REPO / "scripts" / "pool-bind.py")
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pin_list_unpin_round_trip(self):
        self.assertEqual(self.cli.main(["pin", "chan-X", "worker-2"],
                                       workspace=self.ws), 0)
        table = json.loads(
            (self.ws / "state" / "pool" / "affinity.json").read_text())
        self.assertEqual(table["chan-X"]["instance"], "worker-2")
        self.assertTrue(table["chan-X"]["pinned"])
        self.assertEqual(self.cli.main(["unpin", "chan-X"],
                                       workspace=self.ws), 0)
        self.assertEqual(self.cli.main(["unpin", "chan-X"],
                                       workspace=self.ws), 1)

    def test_bad_usage_exits_2(self):
        self.assertEqual(self.cli.main([], workspace=self.ws), 2)
        self.assertEqual(self.cli.main(["pin", "only-channel"],
                                       workspace=self.ws), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
