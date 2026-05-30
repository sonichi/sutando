"""Tests for src/task_archive.py — find_task_file() + archive_file() helpers.

Covers:
- ``find_task_file()`` — #933 (the locator handling ``.claimed-core-N`` rename)
- ``archive_file()`` — #1335 sub-PR-1 (shared archive primitive)
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from task_archive import find_task_file, archive_file


class TestFindTaskFile(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tasks_dir = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _write(self, name: str, content: str = "task body") -> Path:
        p = self.tasks_dir / name
        p.write_text(content)
        return p

    def test_bare_file_returned(self) -> None:
        self._write("task-123.txt")
        result = find_task_file(self.tasks_dir, "task-123")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "task-123.txt")

    def test_claimed_file_returned_when_bare_missing(self) -> None:
        self._write("task-456.claimed-core-2.txt")
        result = find_task_file(self.tasks_dir, "task-456")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "task-456.claimed-core-2.txt")

    def test_bare_preferred_over_claimed(self) -> None:
        self._write("task-789.txt")
        self._write("task-789.claimed-core-1.txt")
        result = find_task_file(self.tasks_dir, "task-789")
        self.assertEqual(result.name, "task-789.txt")

    def test_returns_none_when_no_file(self) -> None:
        result = find_task_file(self.tasks_dir, "task-nonexistent")
        self.assertIsNone(result)

    def test_multiple_claimed_returns_first_lexicographic(self) -> None:
        self._write("task-000.claimed-core-2.txt")
        self._write("task-000.claimed-core-3.txt")
        result = find_task_file(self.tasks_dir, "task-000")
        self.assertIsNotNone(result)
        self.assertIn("claimed-core-", result.name)


class TestArchiveFile(unittest.TestCase):
    """Tests for the shared archive_file helper. Behavioral contract in
    docs/bridge-helpers-design.md § task-archive helper. The TypeScript
    counterpart `archiveFile()` must satisfy the same contract — see
    tests/task-archive-parity.test.py for the cross-language assertion."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.tasks_dir = self.base / "tasks"
        self.tasks_dir.mkdir()
        self.results_dir = self.base / "results"
        self.results_dir.mkdir()
        self.addCleanup(self._td.cleanup)

    def _archive_dir(self, kind: str) -> Path:
        from datetime import datetime
        ym = datetime.now().strftime("%Y-%m")
        return self.base / kind / "archive" / ym

    def test_moves_task_file(self) -> None:
        src = self.tasks_dir / "task-1.txt"
        src.write_text("body")
        archive_file(src, "tasks", "task-1", base=self.base)
        self.assertFalse(src.exists(), "src should be moved")
        dest = self._archive_dir("tasks") / "task-1.txt"
        self.assertTrue(dest.exists(), f"dest should exist at {dest}")
        self.assertEqual(dest.read_text(), "body")

    def test_moves_result_file(self) -> None:
        src = self.results_dir / "task-2.txt"
        src.write_text("result-body")
        archive_file(src, "results", "task-2", base=self.base)
        self.assertFalse(src.exists())
        dest = self._archive_dir("results") / "task-2.txt"
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_text(), "result-body")

    def test_silent_noop_when_src_missing(self) -> None:
        missing = self.tasks_dir / "task-missing.txt"
        # Should not raise:
        archive_file(missing, "tasks", "task-missing", base=self.base)
        # No archive dir should have been created (because we never got
        # past the existence check):
        self.assertFalse(self._archive_dir("tasks").exists())

    def test_creates_archive_dir_recursively(self) -> None:
        # No archive dir initially.
        src = self.tasks_dir / "task-3.txt"
        src.write_text("body")
        self.assertFalse(self._archive_dir("tasks").exists())
        archive_file(src, "tasks", "task-3", base=self.base)
        self.assertTrue(self._archive_dir("tasks").exists())

    def test_idempotent_second_call_noops(self) -> None:
        src = self.tasks_dir / "task-4.txt"
        src.write_text("body")
        archive_file(src, "tasks", "task-4", base=self.base)
        self.assertFalse(src.exists())
        # Second call: src is gone, should silently no-op.
        archive_file(src, "tasks", "task-4", base=self.base)
        # And the archived file is still there with the original content:
        dest = self._archive_dir("tasks") / "task-4.txt"
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_text(), "body")

    def test_falls_back_to_unlink_when_move_fails(self) -> None:
        """If the move operation fails (e.g. destination is unwritable),
        the helper should unlink the source rather than leave it stranded."""
        src = self.tasks_dir / "task-5.txt"
        src.write_text("body")
        # Make the *archive base* unwritable so mkdir(parents=True) fails.
        # We do this by setting the kind dir as a non-directory file:
        bad_kind_dir = self.base / "tasks-bad"
        bad_kind_dir.write_text("not-a-dir")  # blocks mkdir on "tasks-bad/archive/..."
        # Now point archive_file at base=self.base but with kind that
        # resolves through the bad_kind_dir path. Simulate by passing a
        # base that puts the archive on top of an existing file:
        evil_base = self.base / "evil"
        evil_base.write_text("not-a-dir")  # base path is a file, not a dir
        archive_file(src, "tasks", "task-5", base=evil_base)
        # src should have been unlinked (fallback):
        self.assertFalse(src.exists(), "src should be unlinked after move failure")


if __name__ == "__main__":
    unittest.main(verbosity=2)
