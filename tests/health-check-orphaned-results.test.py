#!/usr/bin/env python3
"""Regression coverage for check_orphaned_results.

A result written after its task was archived is claimed by nobody: every
consumer keys off a tracked task_id or a `task-*` glob it has already retired.
The reply exists on disk and is never delivered, and no other check sees it —
`check_task_queue` watches the inbound side, so a queue that drains perfectly
can still be losing every late reply.

Both directions are covered on purpose. A detector that only ever says "warn"
carries no information, so the negative cases (task still queued, result too
fresh, other namespaces) matter as much as the positive one.

Run: python3 tests/health-check-orphaned-results.test.py
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "health_check", REPO / "src" / "health-check.py"
)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

TWO_HOURS_AGO = 7200


class OrphanedResultsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "results").mkdir()
        (self.ws / "tasks").mkdir()
        self._saved = hc.WORKSPACE_DIR
        hc.WORKSPACE_DIR = self.ws

    def tearDown(self) -> None:
        hc.WORKSPACE_DIR = self._saved
        self._tmp.cleanup()

    def _write(self, rel: str, age_sec: int = 0) -> Path:
        path = self.ws / rel
        path.write_text("body")
        if age_sec:
            stamp = time.time() - age_sec
            os.utime(path, (stamp, stamp))
        return path

    # --- positive -------------------------------------------------------

    def test_old_result_without_task_is_orphaned(self):
        self._write("results/task-abc.txt", TWO_HOURS_AGO)
        result = hc.check_orphaned_results()
        self.assertEqual(result["status"], "warn")
        self.assertIn("task-abc.txt", result["detail"])

    def test_counts_all_orphans_and_reports_the_oldest(self):
        self._write("results/task-new.txt", 1000)
        self._write("results/task-old.txt", 20000)
        result = hc.check_orphaned_results()
        self.assertEqual(result["status"], "warn")
        self.assertIn("2 result(s)", result["detail"])
        self.assertIn("task-old.txt", result["detail"])

    # --- negative: the guards that keep a 'warn' meaningful -------------

    def test_task_still_queued_is_not_an_orphan(self):
        """The consumer simply has not reached this pair yet."""
        self._write("results/task-abc.txt", TWO_HOURS_AGO)
        self._write("tasks/task-abc.txt", TWO_HOURS_AGO)
        self.assertEqual(hc.check_orphaned_results()["status"], "ok")

    def test_fresh_result_is_not_an_orphan(self):
        """Between our write and the claim, a few seconds of absence is normal."""
        self._write("results/task-abc.txt")
        self.assertEqual(hc.check_orphaned_results()["status"], "ok")

    def test_pull_namespace_result_is_not_counted(self):
        """`<channel-key>.task-<id>.txt` belongs to a consumer that did not delegate."""
        self._write("results/phone-CA123.task-abc.txt", TWO_HOURS_AGO)
        self.assertEqual(hc.check_orphaned_results()["status"], "ok")

    def test_other_result_lifecycles_are_not_counted(self):
        self._write("results/question-1.txt", TWO_HOURS_AGO)
        self._write("results/proactive-1.txt", TWO_HOURS_AGO)
        self.assertEqual(hc.check_orphaned_results()["status"], "ok")

    def test_missing_results_dir_is_ok(self):
        for entry in (self.ws / "results").iterdir():
            entry.unlink()
        (self.ws / "results").rmdir()
        self.assertEqual(hc.check_orphaned_results()["status"], "ok")

    def test_registered_in_the_check_run(self):
        """A check nothing calls cannot warn about anything."""
        source = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_orphaned_results())", source)


if __name__ == "__main__":
    unittest.main()
