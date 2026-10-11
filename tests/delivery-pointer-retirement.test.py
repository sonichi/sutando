#!/usr/bin/env python3
"""A worker inbox pointer is retired in the step that archives its task body.

`task_archive.archive_file` (every chat bridge) and the gateway's `_archive_result`
move a pointer into `deliveries/<r>/archive/` when they archive the task, and
`retire_archived_pointers` is the idempotent migration for pointers whose task was
archived before this existed. A pending pointer (body still in tasks/) is never moved.

Run: python3 tests/delivery-pointer-retirement.test.py
"""
from __future__ import annotations
# ruff: noqa: E402

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "worker-pool" / "scripts"))

from delivery import task_dispatch as td
import pool_delivery as pd
import task_archive as ta

W = "0123456789abcdef0123456789abcdef"


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ws = Path(self._td.name).resolve()
        self.tasks = self.ws / "tasks"
        self.inbox = self.ws / "deliveries" / W
        for d in (self.tasks / "archive", self.inbox, self.ws / "results"):
            d.mkdir(parents=True, exist_ok=True)

    def task(self, tid, live=True):
        p = (self.tasks if live else self.tasks / "archive") / f"{tid}.txt"
        p.write_text(f"id: {tid}\ntask: x\n")
        return p

    def pointer(self, tid, suffix=".txt"):
        p = self.inbox / f"{tid}{suffix}"
        p.write_text("")
        return p


class RetireOnArchive(Base):
    def test_archive_file_retires_the_pointer_in_the_same_step(self):
        src = self.task("task-a1")
        ptr = self.pointer("task-a1")
        self.assertTrue(ta.archive_file(src, "tasks", "task-a1", tasks_dir=self.tasks / "archive",
                                        results_dir=self.ws / "results" / "archive", log=lambda m: None))
        self.assertFalse(src.exists())
        self.assertFalse(ptr.exists())
        self.assertTrue((self.inbox / "archive" / "task-a1.txt").is_file())

    def test_a_result_archive_leaves_the_pointer(self):
        res = self.ws / "results" / "task-a2.txt"
        res.write_text("answer\n")
        ptr = self.pointer("task-a2")
        ta.archive_file(res, "results", "task-a2", tasks_dir=self.tasks / "archive",
                        results_dir=self.ws / "results" / "archive", log=lambda m: None)
        self.assertTrue(ptr.exists())

    def test_an_accepted_pointer_is_retired_too(self):
        ptr = self.pointer("task-a3", ".accepted")
        moved = ta.retire_delivery_pointers(self.ws / "deliveries", "task-a3", log=lambda m: None)
        self.assertEqual([Path(m).name for m in moved], ["task-a3.accepted"])
        self.assertFalse(ptr.exists())

    def test_no_pointer_is_a_no_op_that_creates_nothing(self):
        self.assertEqual(ta.retire_delivery_pointers(self.ws / "deliveries", "task-none"), [])
        self.assertFalse((self.inbox / "archive").exists())
        self.assertFalse((self.inbox / ta.POINTER_LOCK_NAME).exists())
        self.assertEqual(ta.retire_delivery_pointers(self.ws / "absent", "task-none"), [])

    def test_a_symlinked_inbox_is_never_followed(self):
        real = Path(self._td.name) / "elsewhere"
        real.mkdir()
        (real / "task-a4.txt").write_text("")
        os.symlink(real, self.ws / "deliveries" / "fedcba9876543210fedcba9876543210")
        ta.retire_delivery_pointers(self.ws / "deliveries", "task-a4", log=lambda m: None)
        self.assertTrue((real / "task-a4.txt").exists())

    def test_a_traversal_id_is_refused(self):
        self.assertEqual(ta.retire_delivery_pointers(self.ws / "deliveries", "../x"), [])

    def test_a_held_lock_fails_soft_and_is_logged(self):
        import fcntl
        self.pointer("task-a5")
        logs = []
        with open(self.inbox / ta.POINTER_LOCK_NAME, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            moved = ta.retire_delivery_pointers(self.ws / "deliveries", "task-a5", log=logs.append)
        self.assertEqual(moved, [])
        self.assertTrue((self.inbox / "task-a5.txt").exists())
        self.assertTrue(any("task-a5" in m for m in logs))

    def test_a_retired_pointer_is_no_longer_a_hold_or_a_delivery(self):
        self.pointer("task-a6")
        ta.retire_delivery_pointers(self.ws / "deliveries", "task-a6", log=lambda m: None)
        self.assertFalse(td.worker_holds(self.ws / "deliveries", "task-a6.txt"))
        self.assertEqual(td.owned_task_ids(self.ws / "deliveries", W), [])
        self.assertEqual(pd.pending(self.ws, W), [])
        self.assertEqual(sorted(p.name for p in self.inbox.glob("*.txt")), [])


class Migration(Base):
    def test_retires_archived_keeps_pending_and_unarchived_and_is_idempotent(self):
        for i in range(5):
            self.task(f"task-done{i}", live=False)
            self.pointer(f"task-done{i}")
        month = self.tasks / "archive" / "2026-09"
        month.mkdir()
        (month / "task-month.txt").write_text("id: task-month\n")
        self.pointer("task-month", ".accepted")
        self.task("task-live")
        self.pointer("task-live")
        self.pointer("task-ghost")
        first = ta.retire_archived_pointers(self.ws, log=lambda m: None)
        self.assertEqual(first, {"retired": 6, "kept_pending": 1, "kept_unarchived": 1})
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir() if p.is_file()),
                         [".lock", "task-ghost.txt", "task-live.txt"])
        second = ta.retire_archived_pointers(self.ws, log=lambda m: None)
        self.assertEqual(second, {"retired": 0, "kept_pending": 1, "kept_unarchived": 1})

    def test_cli(self):
        self.task("task-c1", live=False)
        self.pointer("task-c1")
        out = subprocess.run([sys.executable, str(REPO / "src" / "task_archive.py"),
                              "retire-archived-pointers", str(self.ws)],
                             capture_output=True, text=True, check=True).stdout
        self.assertIn('"retired": 1', out)


class OneLayout(unittest.TestCase):
    def test_pointer_names_and_lock_match_the_pool_writer_and_the_hold_reader(self):
        self.assertEqual(ta.POINTER_LOCK_NAME, pd.LOCK_NAME)
        self.assertEqual(set(ta.POINTER_SUFFIXES), set(td._WORKER_HOLD_SUFFIXES))
        self.assertEqual(set(ta.POINTER_SUFFIXES),
                         {pd.PENDING_SUFFIX, pd.ACCEPTED_SUFFIX, pd.LEGACY_ACCEPTED_SUFFIX})

    def test_the_gateway_archive_step_retires_through_the_shared_owner(self):
        src = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py").read_text()
        body = src.split("def _archive_result(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("retire_delivery_pointers(deliveries_root_for(TASKS_DIR), tid", body)

    def test_the_vendored_copy_is_the_same_module(self):
        self.assertEqual((REPO / "src" / "task_archive.py").read_text(),
                         (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "task_archive.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
