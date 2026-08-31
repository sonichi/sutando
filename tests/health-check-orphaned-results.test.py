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
import pathlib
import tempfile
import time
import unittest
from unittest import mock
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
        """Not-reached-yet is transient, so this is scoped to a fresh task."""
        self._write("results/task-abc.txt", TWO_HOURS_AGO)
        self._write("tasks/task-abc.txt")
        self.assertEqual(hc.check_orphaned_results()["status"], "ok")

    def test_aged_unclaimed_task_beside_its_result_IS_an_orphan(self):
        """An unclaimed pair past the threshold is stranded, not in flight."""
        self._write("results/task-newsradar-1.txt", TWO_HOURS_AGO)
        self._write("tasks/task-newsradar-1.txt", TWO_HOURS_AGO)
        result = hc.check_orphaned_results()
        self.assertEqual(result["status"], "warn", result)
        self.assertIn("task-newsradar-1.txt", result["detail"])

    def test_unmeasurable_task_age_warns_rather_than_dropping_the_result(self):
        """An unreadable task age is partial coverage, never a clean pass."""
        self._write("results/task-stat.txt", TWO_HOURS_AGO)

        class _UnstatableTask:
            # Deterministic counterpart to the real-locator control below:
            # patching Path.stat globally would also break Path.exists().
            name = "task-stat.txt"

            def stat(self, *a, **k):
                raise OSError("EIO")

        with mock.patch.object(hc, "find_task_file", return_value=_UnstatableTask()):
            result = hc.check_orphaned_results()
        self.assertEqual(result["status"], "warn", result)
        self.assertIn("unreadable", result["detail"])

    def test_real_locator_raising_does_not_abort_the_probe(self):
        """Drives the REAL find_task_file, whose exists() stats the path too."""
        self._write("results/task-real.txt", TWO_HOURS_AGO)
        task = self._write("tasks/task-real.txt", TWO_HOURS_AGO)
        real_stat = pathlib.Path.stat

        def boom(self_p, *a, **k):
            if self_p == task:
                raise OSError("EIO")
            return real_stat(self_p, *a, **k)

        # No stub: the failure must survive the locator, not bypass it.
        with mock.patch.object(pathlib.Path, "stat", boom):
            result = hc.check_orphaned_results()
        # 3.12 raises out of exists(); 3.14 swallows it and the pair reads as
        # an orphan. Both must warn; only the route differs.
        self.assertEqual(result["status"], "warn", result)

    def test_aged_CLAIMED_task_is_still_not_an_orphan(self):
        """A claimed task is owned by a running consumer, however long it runs."""
        self._write("results/task-slow.txt", TWO_HOURS_AGO)
        self._write("tasks/task-slow.claimed-core-2.txt", TWO_HOURS_AGO)
        self.assertEqual(hc.check_orphaned_results()["status"], "ok")

    def test_claimed_task_is_still_a_live_task(self):
        """`claim_task.py` renames a claimed task — a bare-name test calls it archived.

        This is the peer-review finding on the first cut: a still-live claimed
        task with an older result was reported "never delivered", so a valid
        in-flight/retrying delivery raised the same high-severity signal as a
        genuinely stranded reply — which is how a detector teaches its readers
        to ignore it. The question is "does a task with this id exist", not "is
        there a file with this exact name".
        """
        self._write("results/task-abc.txt", TWO_HOURS_AGO)
        self._write("tasks/task-abc.claimed-core-1.txt", TWO_HOURS_AGO)
        self.assertEqual(hc.check_orphaned_results()["status"], "ok")

    def test_claimed_by_a_different_core_is_also_live(self):
        """The claim suffix carries a core number — do not match only core-1."""
        self._write("results/task-xyz.txt", TWO_HOURS_AGO)
        self._write("tasks/task-xyz.claimed-core-7.txt", TWO_HOURS_AGO)
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

    # --- the "cannot answer" paths ---------------------------------------
    #
    # These are the branches that decide what happens when the probe itself
    # fails. Left untested they are exactly where a check quietly starts
    # reporting "ok" about a directory it never managed to read.

    def test_non_file_entry_is_skipped(self):
        """A directory that happens to match the glob is not a result."""
        (self.ws / "results" / "task-adir.txt").mkdir()
        self.assertEqual(hc.check_orphaned_results()["status"], "ok")

    def test_one_unreadable_entry_does_not_hide_a_real_orphan(self):
        """A single bad entry must not decide the answer for the directory.

        The first cut wrapped the whole loop in one try/except, so one
        unreadable file aborted the scan and returned "could not scan" — a real
        orphan sitting beside it went unreported. pathlib only swallows a
        specific errno set, so `is_file()` genuinely raises for the rest.
        """
        self._write("results/task-bad.txt", TWO_HOURS_AGO)
        self._write("results/task-realorphan.txt", TWO_HOURS_AGO)
        real_stat = Path.stat

        def boom(self_path, *a, **kw):
            if self_path.name == "task-bad.txt":
                raise OSError("permission denied")
            return real_stat(self_path, *a, **kw)

        with mock.patch.object(Path, "stat", boom):
            result = hc.check_orphaned_results()
        self.assertEqual(result["status"], "warn")
        self.assertIn("task-realorphan.txt", result["detail"])
        self.assertIn("1 entry unreadable", result["detail"])

    def test_unreadable_entry_alone_still_warns_never_reports_clean(self):
        """No orphans found, but the scan was incomplete — say so, do not round to ok."""
        self._write("results/task-bad.txt", TWO_HOURS_AGO)
        real_stat = Path.stat

        def boom(self_path, *a, **kw):
            if self_path.name == "task-bad.txt":
                raise OSError("permission denied")
            return real_stat(self_path, *a, **kw)

        with mock.patch.object(Path, "stat", boom):
            result = hc.check_orphaned_results()
        self.assertEqual(result["status"], "warn")
        self.assertIn("unreadable", result["detail"])

    def test_unscannable_results_dir_warns_rather_than_reporting_clean(self):
        """A directory we could not read is UNKNOWN, and must say so out loud.

        The failure mode this guards against is the silent one: swallowing the
        error and returning "no undeliverable results" would claim evidence of
        delivery from a scan that never happened.
        """
        with mock.patch.object(Path, "glob", side_effect=OSError("permission denied")):
            result = hc.check_orphaned_results()
        self.assertEqual(result["status"], "warn")
        self.assertIn("could not scan", result["detail"])

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
