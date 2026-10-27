#!/usr/bin/env python3
"""result_publish.py refuses a cron task's result that no bridge would deliver.

A `source: cron` task with no channel_id/chat_id has no consumer, so a plain body
is archived unsent, even with a [channel:] redirect. The CLI refuses it (exit 2,
nothing written) unless the body is a skip marker, the task names a destination, the result is not in
the canonical workspace's results/, or the caller passes --allow-undeliverable.
A missing or unreadable task file, or a helper that will not import, publishes.

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

sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
from ag2_sparrow import result_publish as vendored_publish  # noqa: E402

# The bridge imports the vendored copy, so every in-process decision runs on both.
MODULES = (result_publish, vendored_publish)

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

    def test_bare_redirect_on_channel_less_cron_task_is_refused(self):
        self._task("task-cron-redirect", CRON_TASK)
        proc, target = self._publish("task-cron-redirect", "[channel: 1234567890]\nreport\n")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertFalse(target.exists())

    def test_redirect_on_routed_task_publishes(self):
        self._task("task-cron-redirect-routed", ROUTED_TASK)
        body = "[channel: 1234567890]\nreport\n"
        proc, target = self._publish("task-cron-redirect-routed", body)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), body)

    def test_skip_markers_publish(self):
        for i, body in enumerate(("[no-send] delivered via owner DM\n", "[REPLIED] posted\n",
                                  "[deduped: task-other-1]\n")):
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

    def test_in_process_decisions_on_both_copies(self):
        self._task("task-cron-a", CRON_TASK)
        self._task("task-cron-routed", ROUTED_TASK)
        self._task("task-chat-b", "id: task-chat-b\nsource: chat\ntask: x\n")
        (self.ws / "tasks" / "task-cron-dir.txt").mkdir()
        res = self.ws / "results"
        cases = [
            (res / "task-cron-a.txt", "report\n", True),
            (res / "task-cron-a.txt", "[no-send] x\n", False),
            (res / "task-cron-a.txt", "[channel: 1234567890]\nreport\n", True),
            (res / "task-cron-routed.txt", "[channel: 1234567890]\nreport\n", False),
            (res / "task-cron-routed.txt", "report\n", False),
            (res / "task-chat-b.txt", "report\n", False),
            (res / "task-cron-missing.txt", "report\n", False),
            # unreadable task file: fail open
            (res / "task-cron-dir.txt", "report\n", False),
            (res / "proactive-1.txt", "report\n", False),
            (self.ws / "elsewhere" / "task-cron-a.txt", "report\n", False),
        ]
        with mock.patch.dict("os.environ", self._env()):
            for mod in MODULES:
                for target, body, refused in cases:
                    with self.subTest(mod=mod.__name__, target=target.name, body=body):
                        reason = mod.undeliverable_reason(target, body)
                        self.assertEqual(reason is not None, refused, reason)

    def test_helper_import_failure_publishes(self):
        self._task("task-cron-c", CRON_TASK)
        target = self.ws / "results" / "task-cron-c.txt"
        with mock.patch.dict("os.environ", self._env()):
            for mod in MODULES:
                with self.subTest(mod=mod.__name__), \
                        mock.patch.object(mod, "_helpers", side_effect=ImportError("gone")):
                    self.assertIsNone(mod.undeliverable_reason(target, "report\n"))

    def test_in_process_main_refuses_and_overrides(self):
        self._task("task-cron-hourly-8", CRON_TASK)
        target = self.ws / "results" / "task-cron-hourly-8.txt"
        with mock.patch.dict("os.environ", self._env()):
            for mod in MODULES:
                with self.subTest(mod=mod.__name__), \
                        mock.patch.object(mod.sys, "stderr", io.StringIO()) as err:
                    with mock.patch.object(mod.sys, "stdin", io.StringIO("report\n")):
                        self.assertEqual(mod.main(["result_publish.py", str(target)]), 2)
                    self.assertIn("deliver-owner-dm.py", err.getvalue())
                    self.assertFalse(target.exists())
                    with mock.patch.object(mod.sys, "stdin", io.StringIO("report\n")):
                        self.assertEqual(mod.main(
                            ["result_publish.py", "--allow-undeliverable", str(target)]), 0)
                    self.assertEqual(target.read_text(encoding="utf-8"), "report\n")
                    target.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
