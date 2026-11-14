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

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from _helpers.hermetic_env import hermetic_env, isolate_os_environ

isolate_os_environ()

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


class GatewayArchive(Base):
    def archive(self, tid):
        source = REPO / "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py"
        tree = ast.parse(source.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_archive_task_file")
        namespace = {"TASKS_DIR": self.tasks, "find_task_file": ta.find_task_file,
                     "retire_delivery_pointers": ta.retire_delivery_pointers,
                     "deliveries_root_for": ta.deliveries_root_for, "_log": lambda m: None}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
        return namespace["_archive_task_file"](tid)

    def test_success_retires_pointer(self):
        body = self.task("task-gateway")
        pointer = self.pointer("task-gateway")
        self.assertTrue(self.archive("task-gateway"))
        self.assertFalse(body.exists())
        self.assertFalse(pointer.exists())

    def test_failed_archive_keeps_task_and_pointer_for_retry(self):
        body = self.task("task-gateway")
        pointer = self.pointer("task-gateway")
        with patch.object(Path, "rename", side_effect=OSError("blocked")):
            self.assertFalse(self.archive("task-gateway"))
        self.assertTrue(body.exists())
        self.assertTrue(pointer.exists())


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
        self.assertEqual(first, {"retired": 6, "kept_pending": 1, "kept_unarchived": 1, "failed": 0})
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir() if p.is_file()),
                         [".lock", "task-ghost.txt", "task-live.txt"])
        second = ta.retire_archived_pointers(self.ws, log=lambda m: None)
        self.assertEqual(second, {"retired": 0, "kept_pending": 1, "kept_unarchived": 1, "failed": 0})

    def test_a_task_requeued_after_the_snapshot_keeps_its_pointer(self):
        self.task("task-rq", live=False)
        ptr = self.pointer("task-rq")
        real_root = ta.deliveries_root_for

        def requeue_then_root(tasks):  # after the live snapshot, before any inbox lock
            self.task("task-rq")
            return real_root(tasks)
        with patch.object(ta, "deliveries_root_for", side_effect=requeue_then_root):
            counts = ta.retire_archived_pointers(self.ws, log=lambda m: None)
        self.assertEqual(counts, {"retired": 0, "kept_pending": 1, "kept_unarchived": 0, "failed": 0})
        self.assertTrue(ptr.exists())
        self.assertFalse((self.inbox / ta.POINTER_ARCHIVE_DIR / "task-rq.txt").exists())

    def test_the_archive_step_keeps_the_pointer_of_a_task_live_again(self):
        self.task("task-rq2")
        ptr = self.pointer("task-rq2")
        self.assertEqual(ta.retire_delivery_pointers(self.ws / "deliveries", "task-rq2", log=lambda m: None), [])
        self.assertTrue(ptr.exists())

    def test_an_inbox_it_cannot_lock_fails_the_run_and_a_later_run_retires_it(self):
        import fcntl
        self.task("task-lk", live=False)
        ptr = self.pointer("task-lk")
        fd = os.open(self.inbox / ta.POINTER_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with patch.object(ta, "_POINTER_LOCK_WAIT_S", 0.1):
                counts = ta.retire_archived_pointers(self.ws, log=lambda m: None)
            self.assertEqual((counts["retired"], counts["failed"]), (0, 1))
            run = subprocess.run([sys.executable, str(REPO / "src" / "task_archive.py"),
                                  "retire-archived-pointers", str(self.ws)],
                                 capture_output=True, text=True, env=hermetic_env())
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertIn('"failed": 1', run.stdout)
            self.assertTrue(ptr.exists())
        finally:
            os.close(fd)
        self.assertEqual(ta.retire_archived_pointers(self.ws, log=lambda m: None)["retired"], 1)
        self.assertFalse(ptr.exists())

    def test_cli(self):
        self.task("task-c1", live=False)
        self.pointer("task-c1")
        out = subprocess.run([sys.executable, str(REPO / "src" / "task_archive.py"),
                              "retire-archived-pointers", str(self.ws)],
                             capture_output=True, text=True, check=True, env=hermetic_env()).stdout
        self.assertIn('"retired": 1', out)


class OneLayout(unittest.TestCase):
    def test_pointer_names_and_lock_match_the_pool_writer_and_the_hold_reader(self):
        self.assertEqual(ta.POINTER_LOCK_NAME, pd.LOCK_NAME)
        self.assertEqual(set(ta.POINTER_SUFFIXES), set(td._WORKER_HOLD_SUFFIXES))
        self.assertEqual(set(ta.POINTER_SUFFIXES),
                         {pd.PENDING_SUFFIX, pd.ACCEPTED_SUFFIX, pd.LEGACY_ACCEPTED_SUFFIX})

    def test_the_gateway_archive_step_retires_through_the_shared_owner(self):
        src = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py").read_text()
        body = src.split("def _archive_task_file(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("retire_delivery_pointers(deliveries_root_for(TASKS_DIR), tid", body)

    def test_the_vendored_copy_is_the_same_module(self):
        self.assertEqual((REPO / "src" / "task_archive.py").read_text(),
                         (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "task_archive.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
