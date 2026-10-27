#!/usr/bin/env python3
"""result_publish.py refuses a cron task's result that no bridge would deliver.

A `source: cron` task with no channel_id/chat_id has no consumer, so a plain body
is archived unsent. The CLI refuses it (exit 2, nothing written) unless the body
is a skip marker or redirect, the task names a destination, or the caller passes
--allow-undeliverable. A result whose task file is absent publishes as before.

Run: python3 tests/result-publish-undeliverable.test.py
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "src" / "result_publish.py"
sys.path.insert(0, str(REPO / "src"))

import result_publish  # noqa: E402

CRON_TASK = ("id: {tid}\ntimestamp: 2026-10-26T12:00:00Z\nsource: cron\nuser_id: cron-runner\n"
             "access_tier: owner\npriority: low\ntask: /hourly-report\n")
ROUTED_TASK = ("id: {tid}\nsource: cron\nchannel_id: 1234567890\naccess_tier: owner\n"
               "task: /hourly-report\n")


class RefuseUndeliverable(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name)
        (self.ws / "tasks" / "archive").mkdir(parents=True)
        (self.ws / "results").mkdir()

    def tearDown(self):
        self._td.cleanup()

    def _task(self, tid, template, archived=False):
        d = self.ws / "tasks" / ("archive" if archived else "")
        (d / f"{tid}.txt").write_text(template.format(tid=tid), encoding="utf-8")

    def _publish(self, tid, body, *flags):
        target = self.ws / "results" / f"{tid}.txt"
        proc = subprocess.run([sys.executable, str(CLI), *flags, str(target)],
                              input=body, text=True, capture_output=True, env=self._env())
        return proc, target

    def _env(self):
        return isolated_child_env(SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=str(self.ws))

    def test_cron_task_without_destination_is_refused(self):
        self._task("task-cron-hourly-1", CRON_TASK)
        proc, target = self._publish("task-cron-hourly-1", "Hourly report: 3 P0s resolved.\n")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("deliver-owner-dm.py", proc.stderr)
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1, proc.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(list((self.ws / "results").iterdir()), [])

    def test_archived_cron_task_is_found_and_refused(self):
        self._task("task-cron-hourly-2", CRON_TASK, archived=True)
        proc, target = self._publish("task-cron-hourly-2", "Hourly report\n")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertFalse(target.exists())

    def test_skip_markers_and_redirect_publish(self):
        for i, body in enumerate(("[no-send] delivered via owner DM\n", "[REPLIED] posted\n",
                                  "[deduped: task-other-1]\n", "[channel: 1234567890]\nreport\n")):
            with self.subTest(body=body):
                tid = f"task-cron-hourly-skip{i}"
                self._task(tid, CRON_TASK)
                proc, target = self._publish(tid, body)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(target.read_text(encoding="utf-8"), body)

    def test_routed_task_publishes(self):
        self._task("task-cron-hourly-3", ROUTED_TASK)
        proc, target = self._publish("task-cron-hourly-3", "Hourly report\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), "Hourly report\n")

    def test_missing_task_file_publishes(self):
        proc, target = self._publish("task-cron-hourly-4", "Hourly report\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(target.exists())

    def test_non_cron_task_without_channel_publishes(self):
        self._task("task-chat-5", "id: task-chat-5\nsource: chat\ntask: do it\n")
        proc, target = self._publish("task-chat-5", "done\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(target.exists())

    def test_allow_undeliverable_publishes(self):
        self._task("task-cron-hourly-6", CRON_TASK)
        proc, target = self._publish("task-cron-hourly-6", "Hourly report\n", "--allow-undeliverable")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), "Hourly report\n")

    def test_result_outside_the_workspace_publishes(self):
        self._task("task-cron-hourly-7", CRON_TASK)
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / "results" / "task-cron-hourly-7.txt"
            proc = subprocess.run([sys.executable, str(CLI), str(target)], input="Hourly report\n",
                                  text=True, capture_output=True, env=self._env())
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(target.exists())

    def test_in_process_main_refuses_and_overrides(self):
        self._task("task-cron-hourly-8", CRON_TASK)
        target = self.ws / "results" / "task-cron-hourly-8.txt"
        with mock.patch.dict("os.environ", self._env()), \
                mock.patch.object(result_publish.sys, "stderr", io.StringIO()) as err:
            with mock.patch.object(result_publish.sys, "stdin", io.StringIO("report\n")):
                self.assertEqual(result_publish.main(["result_publish.py", str(target)]), 2)
            self.assertIn("deliver-owner-dm.py", err.getvalue())
            self.assertFalse(target.exists())
            self.assertIsNone(result_publish.undeliverable_reason(target, "[no-send] x\n"))
            with mock.patch.object(result_publish.sys, "stdin", io.StringIO("report\n")):
                self.assertEqual(result_publish.main(
                    ["result_publish.py", "--allow-undeliverable", str(target)]), 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "report\n")

if __name__ == "__main__":
    unittest.main(verbosity=2)
