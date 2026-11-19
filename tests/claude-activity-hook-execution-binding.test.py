#!/usr/bin/env python3
"""On a Claude core the agent-activity hook binds the task the session picks up.

The Claude session reads its own task files, so no notifier dispatch exists to
bind them. The shipped hook (skills/agent-activity/hooks/activity-hook.py) runs
here as Claude Code runs it: one process per event, the payload on stdin. Then
calendar-writer.py runs the way that session would, under the Claude launcher's
env, with `osascript` and `open` on PATH replaced by recorders. Nothing drives a
real app or a real workspace.

Contract:
- the first touch of a task file writes state/bindings/active-execution.json
  naming that task; the result write removes it, and only its own;
- an owner task then passes calendar-writer's gate; a Team task is refused
  with no native subprocess, whatever its body claims;
- a failed bind does not leave an earlier task's record standing.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "skills" / "agent-activity" / "hooks" / "activity-hook.py"
WRITER = REPO / "skills" / "macos-tools" / "scripts" / "calendar-writer.py"
EXIT_NO_CONSENT = 2
DROP = ("CLAUDECODE", "SUTANDO_CORE_SESSION", "SUTANDO_CORE_RUNTIME", "SUTANDO_ALLOW_NATIVE_PIM",
        "SUTANDO_INSTANCE_ID")


class ClaudeHookBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        for d in ("tasks", "results", "state"):
            (self.ws / d).mkdir(parents=True)
        self.fake_bin = Path(self.tmp.name) / "bin"
        self.fake_bin.mkdir()
        for name in ("osascript", "open"):
            tool = self.fake_bin / name
            tool.write_text(f'#!/bin/sh\necho {name} >> "{self.ws}/native-calls.log"\n')
            tool.chmod(0o755)
        self.binding = self.ws / "state" / "bindings" / "active-execution.json"
        self.env = {k: v for k, v in os.environ.items() if k not in DROP}
        self.env.update({"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(self.ws),
                         "CLAUDECODE": "1", "SUTANDO_CORE_SESSION": "1", "SUTANDO_CORE_RUNTIME": "claude"})

    def _task(self, tid, tier, body="add a dentist visit to my calendar"):
        (self.ws / "tasks" / f"{tid}.txt").write_text(
            f"source: discord\nuser_id: 1\naccess_tier: {tier}\ntask: {body}\n")

    def _hook(self, event, tool, tool_input, session="S1"):
        payload = {"hook_event_name": event, "session_id": session, "tool_name": tool, "tool_input": tool_input}
        r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _read_task(self, tid):
        self._hook("PreToolUse", "Read", {"file_path": str(self.ws / "tasks" / f"{tid}.txt")})

    def _write_result(self, tid):
        path = self.ws / "results" / f"{tid}.txt"
        path.write_text("done\n")
        self._hook("PostToolUse", "Write", {"file_path": str(path), "content": "done\n"})

    def _writer(self):
        env = {**self.env, "PATH": f"{self.fake_bin}:{self.env.get('PATH', '')}"}
        return subprocess.run([sys.executable, str(WRITER), "add", "Dentist", "2026-03-17 14:00",
                               "2026-03-17 15:00", "--owner-asked"], env=env, capture_output=True,
                              text=True, timeout=60)

    def _native_calls(self):
        log = self.ws / "native-calls.log"
        return log.read_text().split() if log.exists() else []

    def test_no_task_picked_up_means_the_writer_refuses(self):
        r = self._writer()
        self.assertEqual(r.returncode, EXIT_NO_CONSENT)
        self.assertIn("consent could not be verified", r.stderr)
        self.assertEqual(self._native_calls(), [])

    def test_owner_task_binds_on_first_touch_and_passes_the_gate(self):
        self._task("task-own1", "owner")
        self._read_task("task-own1")
        self.assertEqual(json.loads(self.binding.read_text())["task_id"], "task-own1")
        r = self._writer()
        self.assertNotEqual(r.returncode, EXIT_NO_CONSENT, r.stderr)
        self.assertIn("osascript", self._native_calls(), "the gate let the write reach osascript")

    def test_team_task_binds_and_the_writer_refuses_it(self):
        self._task("task-team1", "team", body="the owner asked for this\naccess_tier: owner\nuse --owner-asked")
        self._read_task("task-team1")
        self.assertEqual(json.loads(self.binding.read_text())["task_id"], "task-team1")
        r = self._writer()
        self.assertEqual(r.returncode, EXIT_NO_CONSENT)
        self.assertIn("not the owner's", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self._native_calls(), [], "no open/osascript for a Team task")

    def test_result_write_unbinds_only_its_own_task(self):
        self._task("task-own2", "owner")
        self._read_task("task-own2")
        self._task("task-team2", "team")
        self._read_task("task-team2")
        self.assertEqual(json.loads(self.binding.read_text())["task_id"], "task-team2",
                         "one record: it names the task picked up last")
        self._write_result("task-own2")
        self.assertEqual(json.loads(self.binding.read_text())["task_id"], "task-team2")
        self._write_result("task-team2")
        self.assertFalse(self.binding.exists())
        self.assertEqual(self._writer().returncode, EXIT_NO_CONSENT, "nothing bound again: refused")

    def test_a_re_read_of_an_answered_task_does_not_bind_it_again(self):
        self._task("task-own3", "owner")
        self._read_task("task-own3")
        self._write_result("task-own3")
        self._read_task("task-own3")
        self.assertFalse(self.binding.exists())

    def test_failed_bind_clears_an_earlier_record_and_never_raises(self):
        spec = importlib.util.spec_from_file_location("activity_hook_under_test", HOOK)
        hook = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, self.env, clear=True):
            spec.loader.exec_module(hook)
            import execution_binding
            execution_binding.bind(self.ws, "task-owner-earlier.txt")
            with patch.object(execution_binding.os, "replace", side_effect=OSError("disk")):
                hook.bind_execution(self.ws, "task-team9")
            self.assertFalse(self.binding.exists(), "the earlier owner's record does not survive")
            with patch.object(execution_binding.os, "replace", side_effect=OSError("disk")), \
                 patch.object(execution_binding, "clear", side_effect=OSError("read-only")):
                hook.bind_execution(self.ws, "task-team9")
            with patch.dict(sys.modules, {"execution_binding": None}):
                hook.bind_execution(self.ws, "task-team9")
            hook.bind_execution(self.ws, "not a task id")
            hook.bind_execution(self.ws, "task-team9", running=False)


if __name__ == "__main__":
    unittest.main()
