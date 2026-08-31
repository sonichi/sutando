#!/usr/bin/env python3
"""Windows process-tree contract for the Signal guest worker."""

import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import signal_guest_handler as handler  # noqa: E402


class FakeProcess:
    pid = 515151

    def __init__(self, *, timeout=False):
        self.returncode = None
        self.timeout = timeout
        self.killed = False

    def communicate(self, timeout=None):
        if self.timeout:
            raise subprocess.TimeoutExpired("claude", timeout or 0)
        self.returncode = 0
        return "answer", ""

    def kill(self):
        self.killed = True


class WindowsWorkerTree(unittest.TestCase):
    def test_uses_taskkill_for_the_tracked_pid(self):
        process = FakeProcess()
        commands = []

        def run(argv, **kwargs):
            commands.append((list(argv), kwargs))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(handler.os, "name", "nt"), \
                mock.patch.object(handler.subprocess, "run", side_effect=run):
            handler._kill_process_tree(process.pid, process)

        self.assertEqual(
            commands[0][0],
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        )
        self.assertFalse(process.killed)

    def test_taskkill_failure_falls_back_to_process_kill(self):
        process = FakeProcess()
        with mock.patch.object(handler.os, "name", "nt"), \
                mock.patch.object(
                    handler.subprocess, "run", side_effect=OSError("taskkill unavailable")
                ):
            handler._kill_process_tree(process.pid, process)
        self.assertTrue(process.killed)

    def test_timeout_delegates_tree_kill_and_publishes_terminal_result(self):
        process = FakeProcess(timeout=True)
        killed = []
        with tempfile.TemporaryDirectory() as td:
            result_dir = Path(td) / "results"
            home = Path(td) / "home"
            home.mkdir()
            task_id = "signal-guest-timeout"
            handler._reset_stopping_for_tests()
            with handler._live_lock:
                handler._live_tasks[task_id] = result_dir
            handler._slots.acquire()

            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(handler.subprocess, "Popen", return_value=process)
                )
                stack.enter_context(mock.patch.object(handler, "_guard", side_effect=lambda s: s))
                stack.enter_context(
                    mock.patch.object(
                        handler,
                        "_kill_process_tree",
                        side_effect=lambda worker_id, proc=None: killed.append(worker_id),
                    )
                )
                if hasattr(handler.os, "getpgid"):
                    stack.enter_context(
                        mock.patch.object(handler.os, "getpgid", return_value=process.pid)
                    )
                handler._run(task_id, "slow", result_dir, lambda text: text, str(home))

            self.assertEqual(killed, [process.pid])
            self.assertEqual(
                (result_dir / f"{task_id}.txt").read_text(),
                "[deep_dive returned no result]",
            )


if __name__ == "__main__":
    unittest.main()
