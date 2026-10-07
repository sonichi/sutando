#!/usr/bin/env python3
"""A queued task whose file was archived without a visible result must not block the queue.

Issue #4563: process_announced_queue() only dropped its head once has_result found
a result, so a head archived out of tasks/ was retried forever and every newer
task waited behind it. Runs the real main loop on the shared fake-tmux harness.
Run: python3 tests/claude-task-notifier-archived-head.test.py
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import signal
import subprocess
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_h = _load("claude_task_notifier_harness", "claude-task-notifier.test.py")
_inflight = _load("claude_task_notifier_inflight", "claude-task-notifier-inflight.test.py")
FakeTmuxHarness = _h.FakeTmuxHarness
NOTIFIER = _h.NOTIFIER
IDLE_FOOTER = _h.IDLE_FOOTER
DRAFT_FOOTER = _h.DRAFT_FOOTER


class ArchivedHeadTest(FakeTmuxHarness):
    # The main-loop helper, not the class: subclassing it would re-run its tests here.
    _wait_for_fswatch_attach = _inflight.MainLoopWiringTest._wait_for_fswatch_attach

    def tearDown(self):
        left = self._kill_strays()
        super().tearDown()
        self.assertEqual(left, [], "a fixture watcher or fswatch outlived the test")

    def test_an_archived_head_is_dropped_and_the_next_task_proceeds(self):
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        # A busy composer keeps the first task queued at its only wake.
        self.pane_file.write_text(DRAFT_FOOTER + "\n")
        errf_path = self.root / "notifier.stderr"
        errf = open(errf_path, "w")
        proc = subprocess.Popen(
            ["/bin/bash", str(NOTIFIER)],
            env=self._env({"SUTANDO_NOTIFIER_RETRY_POLL_SEC": "1"}),
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=errf,
            text=True,
            start_new_session=True,
        )

        def with_stderr(msg):
            return msg + "\nsend-keys:\n" + self.sendkeys_log_text() + \
                "\nnotifier stderr:\n" + errf_path.read_text(errors="replace")

        try:
            try:
                self.assertTrue(self._wait_for_fswatch_attach(),
                                with_stderr("fswatch never attached to the watched tasks dir"))
                self.write_task("task-gone.txt")
                time.sleep(1.5)
                self.assertNotIn("TYPE", self.sendkeys_log_text(),
                                 with_stderr("a busy composer must not have been typed over"))
                # Archived out of tasks/ with no result anywhere has_result looks.
                (self.tasks_dir / "task-gone.txt").unlink()
                self.write_task("task-next.txt")
                # Outlast CORE_READY_TIMEOUT (3s here) so a delivery attempt begun
                # before the unlink has given up; the next pick sees the file gone.
                time.sleep(4.5)
                self.assertNotIn("TYPE", self.sendkeys_log_text(),
                                 with_stderr("nothing may be typed while the composer is busy"))
                self.pane_file.write_text(IDLE_FOOTER + "\n")
                deadline = time.time() + 15
                while time.time() < deadline:
                    if "TYPE Sutando task ready: task-next.txt" in self.sendkeys_log_text():
                        break
                    time.sleep(0.2)
                else:
                    self.fail(with_stderr("the task behind an archived head was never delivered"))
                self.assertNotIn("TYPE Sutando task ready: task-gone.txt", self.sendkeys_log_text(),
                                 with_stderr("an archived task must not be typed into the core"))
                self.assertIn("dropped task-gone.txt from the queue", errf_path.read_text(),
                              with_stderr("the drop must be logged"))
                self.write_result("task-next.txt")
                deadline = time.time() + 10
                while time.time() < deadline and proc.poll() is None:
                    time.sleep(0.2)
            finally:
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait(timeout=5)
        finally:
            errf.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
