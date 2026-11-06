#!/usr/bin/env python3
"""find_archived_result / find_result must locate a result in EVERY archive
layout a consumer may meet, with the same epoch-suffix rule in each; the
precedence they share is defined once, by iter_result_candidates, and pinned
here so a completion check can walk past an empty live placeholder without
the first-existing finders changing their answer.

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

from local_task_protocol import (  # noqa: E402
    find_archived_result,
    find_result,
    iter_result_candidates,
)

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


class IterResultCandidatesOrderTest(unittest.TestCase):
    """The single precedence definition: live, flat exact, month dirs newest
    first (exact then epoch re-archives newest first), retention dirs newest
    first (same), flat epoch re-archives newest first. Existence only — an
    empty file is a candidate; readiness is the caller's question."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = Path(self.tmp.name)

    def _touch(self, rel, body="done\n"):
        p = self.results / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        return p

    def test_full_precedence_is_pinned(self):
        expected = [
            self._touch(f"{TASK}.txt", ""),
            self._touch(f"archive/{TASK}.txt"),
            self._touch(f"archive/2026-09/{TASK}.txt", ""),
            self._touch(f"archive/2026-09/{TASK}-1700000001.txt"),
            self._touch(f"archive/2026-09/{TASK}-1700000000.txt"),
            self._touch(f"archive/2026-08/{TASK}.txt"),
            self._touch(f"archive/2026-08/{TASK}-1690000000.txt"),
            self._touch(f"archive-2026-07-26/{TASK}.txt"),
            self._touch(f"archive-2026-07-26/{TASK}-1784690000.txt"),
            self._touch(f"archive-2026-07-25/{TASK}-1784600000.txt"),
            self._touch(f"archive/{TASK}-1784690001.txt"),
            self._touch(f"archive/{TASK}-1784690000.txt"),
        ]
        for decoy in (f"archive/{TASK}-1-other.txt", f"archive/{TASK}-notes.txt",
                      f"archive/{TASK}2.txt", f"archive/2026-09/{TASK}-1700000000x.txt",
                      f"archive-2026-07-26/{TASK}-abc.txt", f"archive/not-a-month/{TASK}.txt",
                      f"archive-2026-07/{TASK}.txt"):
            self._touch(decoy)
        self.assertEqual(list(iter_result_candidates(self.results, TASK)), expected)

    def test_find_result_is_the_first_candidate(self):
        self._touch(f"{TASK}.txt", "")
        self._touch(f"archive/{TASK}.txt")
        self.assertEqual(find_result(self.results, TASK), self.results / f"{TASK}.txt")
        self.assertEqual(find_result(self.results, TASK), next(iter_result_candidates(self.results, TASK)))

    def test_find_archived_result_is_the_first_archived_candidate(self):
        self._touch(f"{TASK}.txt")
        self._touch(f"archive/2026-09/{TASK}-1700000000.txt")
        self._touch(f"archive/{TASK}-1784690000.txt")
        candidates = list(iter_result_candidates(self.results, TASK))
        self.assertEqual(candidates[0], self.results / f"{TASK}.txt")
        self.assertEqual(find_archived_result(self.results, TASK), candidates[1])

    def test_an_empty_live_file_is_a_candidate_not_a_verdict(self):
        live = self._touch(f"{TASK}.txt", "")
        archived = self._touch(f"archive/{TASK}.txt")
        self.assertEqual(list(iter_result_candidates(self.results, TASK)), [live, archived])
        self.assertEqual(find_result(self.results, TASK), live)

    def test_every_single_layout_agrees_with_find_result(self):
        for rel in (f"{TASK}.txt", f"archive/{TASK}.txt", f"archive/2026-09/{TASK}.txt",
                    f"archive/2026-09/{TASK}-1700000000.txt", f"archive-2026-07-26/{TASK}.txt",
                    f"archive-2026-07-26/{TASK}-1784690000.txt", f"archive/{TASK}-1784690000.txt"):
            with self.subTest(layout=rel):
                with tempfile.TemporaryDirectory() as td:
                    results = Path(td)
                    p = results / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text("done\n")
                    self.assertEqual(list(iter_result_candidates(results, TASK)), [p])
                    self.assertEqual(find_result(results, TASK), p)

    def test_missing_and_traversal_ids_yield_nothing(self):
        self.assertEqual(list(iter_result_candidates(self.results, TASK)), [])
        self._touch(f"{TASK}.txt")
        self.assertEqual(list(iter_result_candidates(self.results, "../../etc/passwd")), [])
        self.assertEqual(list(iter_result_candidates(self.results, f"../{TASK}")), [])


if __name__ == "__main__":
    unittest.main()
