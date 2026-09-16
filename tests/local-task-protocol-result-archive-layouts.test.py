#!/usr/bin/env python3
"""find_archived_result / find_result must locate a result in EVERY archive
layout a consumer may meet, with the same epoch-suffix rule in each.

The retention-dir scan (`archive-YYYY-MM-DD/`, a sibling of `archive/`) checked
only the exact filename, so `archive-2026-07-26/task-done-1784690000.txt` read
as "never delivered" and a completed task was replayed — a shape the Codex
notifier's own bash lookup had covered. The epoch suffix is ALL digits: a
leading-digit glob (`<stem>-[0-9]*.txt`) also swallowed another task's
`task-done-1-other.txt`.

Run: python3 tests/local-task-protocol-result-archive-layouts.test.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from local_task_protocol import find_archived_result, find_result  # noqa: E402

TASK = "task-done"


class RetentionDirLayoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = Path(self.tmp.name)
        self.day = self.results / "archive-2026-07-26"
        self.day.mkdir()

    def test_exact_name_in_retention_dir(self):
        (self.day / f"{TASK}.txt").write_text("done\n")
        self.assertEqual(find_archived_result(self.results, TASK), self.day / f"{TASK}.txt")

    def test_epoch_suffixed_name_in_retention_dir(self):
        # Regression: this returned None, so the task was replayed.
        (self.day / f"{TASK}-1784690000.txt").write_text("done\n")
        self.assertEqual(
            find_archived_result(self.results, TASK), self.day / f"{TASK}-1784690000.txt")

    def test_exact_name_wins_over_epoch_suffixed_in_same_day(self):
        (self.day / f"{TASK}.txt").write_text("exact\n")
        (self.day / f"{TASK}-1784690000.txt").write_text("suffixed\n")
        self.assertEqual(find_archived_result(self.results, TASK).read_text(), "exact\n")

    def test_newest_epoch_wins_in_retention_dir(self):
        (self.day / f"{TASK}-1784690000.txt").write_text("older\n")
        (self.day / f"{TASK}-1784690001.txt").write_text("newer\n")
        self.assertEqual(find_archived_result(self.results, TASK).read_text(), "newer\n")

    def test_another_tasks_file_is_not_this_tasks_epoch_archive(self):
        # `task-done-1-other` is a different task; `-1-other` is not an epoch.
        (self.day / f"{TASK}-1-other.txt").write_text("other\n")
        self.assertIsNone(find_archived_result(self.results, TASK))
        self.assertEqual(
            find_archived_result(self.results, f"{TASK}-1-other"),
            self.day / f"{TASK}-1-other.txt")

    def test_non_digit_suffix_is_not_an_epoch(self):
        (self.day / f"{TASK}-1784690000x.txt").write_text("x\n")
        (self.day / f"{TASK}-abc.txt").write_text("x\n")
        self.assertIsNone(find_archived_result(self.results, TASK))

    def test_newest_day_dir_wins(self):
        older = self.results / "archive-2026-07-25"
        older.mkdir()
        (older / f"{TASK}-1784600000.txt").write_text("older day\n")
        (self.day / f"{TASK}-1784690000.txt").write_text("newer day\n")
        self.assertEqual(find_archived_result(self.results, TASK).read_text(), "newer day\n")

    def test_find_result_reaches_the_retention_epoch_archive(self):
        (self.day / f"{TASK}-1784690000.txt").write_text("done\n")
        self.assertEqual(find_result(self.results, TASK), self.day / f"{TASK}-1784690000.txt")


class OtherLayoutsStillFoundTest(unittest.TestCase):
    """The other three layouts are unchanged; pinned so the fix cannot reorder them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = Path(self.tmp.name)
        (self.results / "archive").mkdir()

    def test_flat_archive(self):
        (self.results / "archive" / f"{TASK}.txt").write_text("done\n")
        self.assertIsNotNone(find_archived_result(self.results, TASK))

    def test_flat_epoch_archive(self):
        (self.results / "archive" / f"{TASK}-1784690000.txt").write_text("done\n")
        self.assertIsNotNone(find_archived_result(self.results, TASK))

    def test_month_archive(self):
        month = self.results / "archive" / "2026-07"
        month.mkdir()
        (month / f"{TASK}.txt").write_text("done\n")
        self.assertIsNotNone(find_archived_result(self.results, TASK))

    def test_month_epoch_archive(self):
        month = self.results / "archive" / "2026-07"
        month.mkdir()
        (month / f"{TASK}-1784690000.txt").write_text("done\n")
        self.assertIsNotNone(find_archived_result(self.results, TASK))

    def test_flat_decoy_is_not_matched(self):
        (self.results / "archive" / f"{TASK}-1-other.txt").write_text("other\n")
        self.assertIsNone(find_archived_result(self.results, TASK))

    def test_traversal_id_is_none(self):
        self.assertIsNone(find_archived_result(self.results, "../../etc/passwd"))
        self.assertIsNone(find_result(self.results, "../../etc/passwd"))


if __name__ == "__main__":
    unittest.main()
