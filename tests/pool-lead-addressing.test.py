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
            lead = _lead(ws, ["worker-1", "worker-2"])
            lead.pin_room("!r:x", "worker-1")
            _task(ws, "task-a1.txt", channel="!r:x", target="worker-2")
            self.assertEqual(lead.sweep(), [("task-a1.txt", "worker-2")])

    def test_unknown_target_degrades_to_normal_routing(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["worker-1"])
            _task(ws, "task-a2.txt", target="worker-9")
            self.assertEqual(lead.sweep(), [("task-a2.txt", "worker-1")])

    def test_unclaiming_target_degrades_instead_of_blackholing(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["worker-1", "worker-2"], claiming={"worker-1"})
            _task(ws, "task-a3.txt", target="worker-2")
            self.assertEqual(lead.sweep(), [("task-a3.txt", "worker-1")])

    def test_fan_out_copies_to_every_claiming_worker(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["worker-1", "worker-2", "worker-3"],
                         claiming={"worker-1", "worker-3"})
            _task(ws, "task-f1.txt", fan_out=True)
            got = sorted(lead.sweep())
            self.assertEqual(got, [
                ("task-f1~worker-1.assigned-worker-1.txt", "worker-1"),
                ("task-f1~worker-3.assigned-worker-3.txt", "worker-3")])
            self.assertFalse((ws / "tasks" / "task-f1.txt").exists())
            self.assertTrue(
                (ws / "tasks" / "archive" / "task-f1.txt").exists(),
                "original retires to archive, never double-assigns")

    def test_fan_out_with_no_claiming_workers_leaves_task(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["worker-1"], claiming=set())
            _task(ws, "task-f2.txt", fan_out=True)
            self.assertEqual(lead.sweep(), [])
            self.assertTrue((ws / "tasks" / "task-f2.txt").exists(),
                            "task waits for a claiming worker")

    def test_plain_task_untouched_by_the_feature(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["worker-1"])
            _task(ws, "task-p1.txt")
            self.assertEqual(lead.sweep(), [("task-p1.txt", "worker-1")])


class AddressingForgeryGuard(unittest.TestCase):
    def test_body_lines_cannot_forge_routing(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["worker-1", "worker-2"])
            # user-controlled BODY smuggles both headers below the delimiter
            (ws / "tasks" / "task-fg.txt").write_text(
                "id: task-fg\nchannel_id: !r:x\ntask: please do\n"
                "target_worker: worker-2\nfan_out: true\n")
            got = lead.sweep()
            self.assertEqual(got, [("task-fg.txt", "worker-1")],
                             "body text must neither target nor fan out")

    def test_real_header_above_delimiter_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["worker-1", "worker-2"])
            _task(ws, "task-ok.txt", target="worker-2")
            self.assertEqual(lead.sweep(), [("task-ok.txt", "worker-2")])


class AddressingIOGuards(unittest.TestCase):
    def test_unreadable_task_reads_as_unaddressed(self):
        from pool_lead import _read_addressing
        self.assertEqual(
            _read_addressing(Path("/nonexistent-addressing-guard")),
            (None, False))

    def test_fan_out_unreadable_original_assigns_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["worker-1", "worker-2"])
            ghost = ws / "tasks" / "task-gone.txt"  # never created
            self.assertEqual(lead._fan_out(ghost, ["worker-1", "worker-2"]), [])

    def test_fan_out_unwritable_copies_assign_nothing_and_keep_original(self):
        import os as _os
        import stat as _stat
        if _os.geteuid() == 0:
            self.skipTest("EACCES not enforceable as root")
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["worker-1", "worker-2"])
            _task(ws, "task-ro.txt", fan_out=True)
            f = ws / "tasks" / "task-ro.txt"
            _os.chmod(ws / "tasks", _stat.S_IRUSR | _stat.S_IXUSR)
            try:
                self.assertEqual(lead._fan_out(f, ["worker-1", "worker-2"]), [])
            finally:
                _os.chmod(ws / "tasks", _stat.S_IRWXU)
            self.assertTrue(f.exists(), "original survives a failed fan-out")

    def test_fan_out_blocked_archive_keeps_the_copies(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["worker-1", "worker-3"])
            _task(ws, "task-ba.txt", fan_out=True)
            (ws / "tasks" / "archive").write_text("a FILE blocks the dir")
            got = sorted(lead._fan_out(ws / "tasks" / "task-ba.txt",
                                       ["worker-1", "worker-3"]))
            self.assertEqual(
                got, [("task-ba~worker-1.assigned-worker-1.txt", "worker-1"),
                      ("task-ba~worker-3.assigned-worker-3.txt", "worker-3")],
                "copies stand even when the archive move fails")

    def test_fan_out_after_failed_archive_retires_instead_of_recopying(self):
        # A stale original + a claimed copy must not become a second
        # assignment: the entry guard retires it once the archive unblocks.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["worker-1", "worker-3"])
            _task(ws, "task-ra.txt", fan_out=True)
            blocker = ws / "tasks" / "archive"
            blocker.write_text("a FILE blocks the dir")
            orig = ws / "tasks" / "task-ra.txt"
            first = lead._fan_out(orig, ["worker-1", "worker-3"])
            self.assertEqual(len(first), 2)
            self.assertTrue(orig.exists(), "archive blocked, original stale")
            assigned = ws / "tasks" / "task-ra~worker-1.assigned-worker-1.txt"
            assigned.rename(ws / "tasks" / "task-ra~worker-1.claimed-worker-1.txt")
            second = lead._fan_out(orig, ["worker-1", "worker-3"])
            self.assertEqual(second, [], "entry guard blocks the re-fan")
            self.assertFalse(assigned.exists(),
                             "the claimed worker gets no second assignment")
            self.assertTrue(orig.exists(), "retire also failed: archive still blocked")
            blocker.unlink()
            third = lead._fan_out(orig, ["worker-1", "worker-3"])
            self.assertEqual(third, [])
            self.assertFalse(orig.exists(), "unblocked: original retired")
            self.assertTrue((ws / "tasks" / "archive" / "task-ra.txt").exists())

    def test_fan_out_guard_sees_consumed_copy_results(self):
        # All copies consumed (results archived), original stale: still no re-fan.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            lead = _lead(ws, ["worker-1"])
            _task(ws, "task-rc.txt", fan_out=True)
            ra = ws / "results" / "archive"
            ra.mkdir(parents=True, exist_ok=True)
            (ra / "task-rc~worker-1.txt").write_text("done")
            got = lead._fan_out(ws / "tasks" / "task-rc.txt", ["worker-1"])
            self.assertEqual(got, [], "per-copy result is fan-out evidence")
            self.assertFalse((ws / "tasks" / "task-rc.txt").exists(),
                             "original retired, not re-fanned")


if __name__ == "__main__":
    unittest.main(verbosity=1)
