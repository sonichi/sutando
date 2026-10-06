#!/usr/bin/env python3
"""The Claude task notifier re-delivers a submitted prompt that an API-error turn lost.

Uses the fake tmux harness from tests/claude-task-notifier.test.py; the policy itself
is pinned in tests/turn-failure-redelivery.test.py.
Run: python3 tests/claude-task-notifier-turn-failure.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "claude_task_notifier_harness", Path(__file__).resolve().parent / "claude-task-notifier.test.py")
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)
FakeTmuxHarness = _h.FakeTmuxHarness
sys.path.insert(0, str(_h.REPO / "src"))

from util_paths import turn_failure_path  # noqa: E402

FAST = {"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "3", "SUTANDO_NOTIFIER_RETRY_CHECK_SEC": "0"}


class LostSubmitTests(FakeTmuxHarness):
    def run_event(self, filename, env_extra=None, timeout=15):
        # Pinned: an inherited SUTANDO_WORKSPACE_DIR would aim the markers at a live workspace.
        env = {"SUTANDO_WORKSPACE_DIR": str(self.state_dir.parent), **(env_extra or {})}
        return super().run_event(filename, env_extra=env, timeout=timeout)

    def submit_once(self, name):
        self.write_task(name)
        first = self.run_event(name, env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        self.assertEqual(first.returncode, 0, first.stderr)
        marker = self.inflight_dir / name
        self.assertTrue(marker.is_file(), "no in-flight marker after the first submit")
        past = time.time() - 120
        os.utime(marker, (past, past))
        return marker

    def write_failure(self, error, recovered):
        path = turn_failure_path(self.state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        path.write_text(json.dumps({"failed_at": now - 60, "error": error, "session_id": "s",
                                    "recovered_at": now if recovered else None}))

    def typed(self, name):
        return self.sendkeys_log_text().count(f"TYPE Sutando task ready: {name}")

    def test_a_prompt_lost_to_a_502_is_released_and_typed_again(self):
        marker = self.submit_once("task-502.txt")
        self.write_failure("server_error", recovered=True)
        second = self.run_event("task-502.txt", env_extra=FAST)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("re-delivering task-502.txt", second.stderr)
        self.assertFalse(marker.exists(), "the lost submit still holds the task")
        def _finish():
            for _ in range(100):
                if self.typed("task-502.txt") >= 2:
                    self.write_result("task-502.txt"); return
                time.sleep(0.1)
        t = threading.Thread(target=_finish); t.start()
        third = self.run_event("task-502.txt", env_extra=FAST)
        t.join(timeout=5)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(self.typed("task-502.txt"), 2, third.stderr)

    def test_an_auth_failure_keeps_waiting_and_never_retypes(self):
        marker = self.submit_once("task-auth.txt")
        self.write_failure("authentication_failed", recovered=True)
        second = self.run_event("task-auth.txt", env_extra=FAST)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("awaiting its result", second.stderr)
        self.assertNotIn("re-delivering", second.stderr)
        self.assertTrue(marker.exists())
        self.assertEqual(self.typed("task-auth.txt"), 1)

    def test_a_failure_older_than_the_submit_does_not_release_it(self):
        self.write_task("task-old.txt")
        self.write_failure("server_error", recovered=True)
        first = self.run_event("task-old.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_event("task-old.txt", env_extra=FAST)
        self.assertNotIn("re-delivering", second.stderr)
        self.assertEqual(self.typed("task-old.txt"), 1)


if __name__ == "__main__":
    unittest.main()
