#!/usr/bin/env python3
"""Per-message worker addressing (owner semantics 2026-08-31): an explicit
target_worker header outranks room bindings and load; fan_out assigns one
copy to every claiming worker and retires the original; a dead/unknown
target degrades to normal routing instead of black-holing the task.

Run: python3 tests/pool-lead-addressing.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_lead import PoolLead  # noqa: E402


def _lead(ws: Path, followers, claiming=None):
    (ws / "tasks").mkdir(exist_ok=True)
    (ws / "state").mkdir(exist_ok=True)
    claiming = set(followers) if claiming is None else set(claiming)
    lead = PoolLead(ws / "tasks", ws / "state",
                    followers_fn=lambda: list(followers),
                    alive_fn=lambda i: True)
    lead._claiming = lambda i: i in claiming
    return lead


def _task(ws, name, channel=None, target=None, fan_out=False):
    lines = [f"id: {name[:-4]}"]
    if channel:
        lines.append(f"channel_id: {channel}")
    if target:
        lines.append(f"target_worker: {target}")
    if fan_out:
        lines.append("fan_out: true")
    (ws / "tasks" / name).write_text("\n".join(lines) + "\ntask: t\n")


class AddressingTests(unittest.TestCase):
    def test_target_outranks_room_binding(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["core-1", "core-2"])
            lead.pin_room("!r:x", "core-1")
            _task(ws, "task-a1.txt", channel="!r:x", target="core-2")
            self.assertEqual(lead.sweep(), [("task-a1.txt", "core-2")])

    def test_unknown_target_degrades_to_normal_routing(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["core-1"])
            _task(ws, "task-a2.txt", target="core-9")
            self.assertEqual(lead.sweep(), [("task-a2.txt", "core-1")])

    def test_unclaiming_target_degrades_instead_of_blackholing(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["core-1", "core-2"], claiming={"core-1"})
            _task(ws, "task-a3.txt", target="core-2")
            self.assertEqual(lead.sweep(), [("task-a3.txt", "core-1")])

    def test_fan_out_copies_to_every_claiming_worker(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["core-1", "core-2", "core-3"],
                         claiming={"core-1", "core-3"})
            _task(ws, "task-f1.txt", fan_out=True)
            got = sorted(lead.sweep())
            self.assertEqual(got, [
                ("task-f1~core-1.assigned-core-1.txt", "core-1"),
                ("task-f1~core-3.assigned-core-3.txt", "core-3")])
            self.assertFalse((ws / "tasks" / "task-f1.txt").exists())
            self.assertTrue(
                (ws / "tasks" / "archive" / "task-f1.txt").exists(),
                "original retires to archive, never double-assigns")

    def test_fan_out_with_no_claiming_workers_leaves_task(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["core-1"], claiming=set())
            _task(ws, "task-f2.txt", fan_out=True)
            self.assertEqual(lead.sweep(), [])
            self.assertTrue((ws / "tasks" / "task-f2.txt").exists(),
                            "task waits for a claiming worker")

    def test_plain_task_untouched_by_the_feature(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["core-1"])
            _task(ws, "task-p1.txt")
            self.assertEqual(lead.sweep(), [("task-p1.txt", "core-1")])


if __name__ == "__main__":
    unittest.main(verbosity=1)
