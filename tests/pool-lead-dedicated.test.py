#!/usr/bin/env python3
"""Dedicated workers: a room's exclusive worker leaves the general rotation,
still serves its own room, never starves an all-reserved pool, and the flag
round-trips through unpin, the CLI, and the status snapshot.

Run: python3 tests/pool-lead-dedicated.test.py   (stdlib only)
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
from pool_status import PoolStatusWriter  # noqa: E402


class DedicatedWorkerTests(unittest.TestCase):
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

    def _task(self, name, channel=None):
        lines = [f"id: {name[:-4]}"]
        if channel:
            lines.append(f"channel_id: {channel}")
        (self.tasks / name).write_text("\n".join(lines) + "\ntask: t\n")

    def test_reserved_worker_leaves_general_rotation(self):
        # core-a would win chan-X on the lexicographic tie; reserving it
        # for chan-D must push chan-X's task to core-b instead
        self.lead.pin_room("chan-D", "core-a", dedicated=True)
        self._task("task-d1.txt", channel="chan-X")
        self.assertEqual(self.lead.sweep(), [("task-d1.txt", "core-b")])

    def test_dedicated_room_still_routes_to_its_worker(self):
        self.lead.pin_room("chan-D", "core-b", dedicated=True)
        self._task("task-d2.txt", channel="chan-D")
        self.assertEqual(self.lead.sweep(), [("task-d2.txt", "core-b")])

    def test_channelless_task_also_avoids_reserved_worker(self):
        self.lead.pin_room("chan-D", "core-a", dedicated=True)
        self._task("task-d3.txt")
        self.assertEqual(self.lead.sweep(), [("task-d3.txt", "core-b")])

    def test_all_reserved_pool_falls_back_instead_of_starving(self):
        self.alive = {"core-a": True}
        self.lead.pin_room("chan-D", "core-a", dedicated=True)
        self._task("task-d4.txt", channel="chan-X")
        out = self.lead.sweep()
        self.assertEqual(out, [("task-d4.txt", "core-a")],
                         "availability beats exclusivity when nothing is free")

    def test_plain_pin_does_not_reserve(self):
        # negative control: a particular (non-dedicated) pin keeps its worker
        # in rotation for other channels
        self.lead.pin_room("chan-D", "core-a", dedicated=False)
        self._task("task-d5.txt", channel="chan-X")
        picks = dict(self.lead.sweep())
        self.assertEqual(picks["task-d5.txt"], "core-a",
                         "tie still goes to core-a: no reservation applied")

    def test_unpin_releases_the_reservation(self):
        self.lead.pin_room("chan-D", "core-a", dedicated=True)
        self.assertTrue(self.lead.unpin_room("chan-D"))
        row = self.lead.bindings()["chan-D"]
        self.assertNotIn("pinned", row)
        self.assertNotIn("exclusive", row)
        self._task("task-d6.txt", channel="chan-X")
        picks = dict(self.lead.sweep())
        self.assertEqual(picks["task-d6.txt"], "core-a")

    def test_snapshot_carries_dedicated_flag(self):
        self.lead.pin_room("chan-D", "core-b", dedicated=True)
        self.lead.pin_room("chan-P", "core-a")
        w = PoolStatusWriter(self.tasks, self.state, lambda: ["core-a"],
                             lambda i: True, now_fn=lambda: self.clock[0],
                             bindings_fn=self.lead.bindings)
        self.assertTrue(w.maybe_write())
        got = json.loads(
            (self.state / "pool-status.json").read_text())["bindings"]
        self.assertTrue(got["chan-D"]["dedicated"])
        self.assertFalse(got["chan-P"]["dedicated"])

    def test_cli_dedicated_flag_round_trip(self):
        spec = importlib.util.spec_from_file_location(
            "pool_bind", REPO / "scripts" / "pool-bind.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        ws = Path(self.tmp.name)
        self.assertEqual(
            cli.main(["pin", "chan-D", "core-2", "--dedicated"],
                     workspace=ws), 0)
        table = json.loads(
            (ws / "state" / "pool" / "affinity.json").read_text())
        self.assertTrue(table["chan-D"]["exclusive"])
        self.assertTrue(table["chan-D"]["pinned"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
