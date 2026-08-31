#!/usr/bin/env python3
"""Multi-worker room binding (pool-restriction semantics): a pinned SET is
the room's whole pool — the lead routes each task to its least-loaded
claiming member, skips busy/dead members, and loans only when the entire
set cannot claim. Unpin clears the set.

Run: python3 tests/pool-lead-multi-bind.test.py   (stdlib only)
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


class MultiBindTests(unittest.TestCase):
    def test_pin_set_persists_and_lists(self):
        with tempfile.TemporaryDirectory() as td:
            lead = _lead(Path(td), ["core-1", "core-2", "core-3"])
            row = lead.pin_room("!r:x", ["core-2", "core-3"])
            self.assertEqual(row["instances"], ["core-2", "core-3"])
            self.assertTrue(row["pinned"])
            self.assertEqual(
                lead.bindings()["!r:x"]["instances"], ["core-2", "core-3"])

    def test_single_pin_stays_compact_legacy_form(self):
        with tempfile.TemporaryDirectory() as td:
            lead = _lead(Path(td), ["core-1"])
            row = lead.pin_room("!r:x", "core-1")
            self.assertNotIn("instances", row)
            self.assertEqual(row["instance"], "core-1")

    def test_sweep_routes_within_the_set_only(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["core-1", "core-2", "core-3"])
            lead.pin_room("!r:x", ["core-2", "core-3"])
            for i in range(4):
                (ws / "tasks" / f"task-m{i}.txt").write_text(
                    f"id: task-m{i}\nchannel_id: !r:x\ntask: t\n")
            picks = {inst for _n, inst in lead.sweep()}
            self.assertTrue(picks and picks <= {"core-2", "core-3"}, picks)

    def test_busy_member_yields_to_the_other(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["core-1", "core-2", "core-3"])
            lead.pin_room("!r:x", ["core-2", "core-3"])
            # load core-2 with an in-flight claim; core-3 idle
            (ws / "tasks" / "task-b.claimed-core-2.txt").write_text("x")
            (ws / "tasks" / "task-y.txt").write_text(
                "id: task-y\nchannel_id: !r:x\ntask: t\n")
            self.assertEqual(lead.sweep(), [("task-y.txt", "core-3")])

    def test_whole_set_not_claiming_falls_through_to_loan(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["core-1", "core-2", "core-3"],
                         claiming={"core-1"})
            lead.pin_room("!r:x", ["core-2", "core-3"])
            (ws / "tasks" / "task-z.txt").write_text(
                "id: task-z\nchannel_id: !r:x\ntask: t\n")
            self.assertEqual(lead.sweep(), [("task-z.txt", "core-1")],
                             "availability beats the binding when the whole "
                             "set is unclaiming")
            self.assertEqual(
                lead.bindings()["!r:x"]["instances"], ["core-2", "core-3"],
                "the loan must not consume the pin")

    def test_unpin_clears_the_set(self):
        with tempfile.TemporaryDirectory() as td:
            lead = _lead(Path(td), ["core-1", "core-2"])
            lead.pin_room("!r:x", ["core-1", "core-2"])
            self.assertTrue(lead.unpin_room("!r:x"))
            row = lead.bindings()["!r:x"]
            self.assertNotIn("instances", row)
            self.assertNotIn("pinned", row)

    def test_empty_set_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            lead = _lead(Path(td), ["core-1"])
            with self.assertRaises(ValueError):
                lead.pin_room("!r:x", [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
