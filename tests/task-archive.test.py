"""Tests for src/task_archive.py — find_task_file() helper (closes #933)."""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

import os
from datetime import datetime

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from task_archive import archive_file, find_task_file


class TestArchiveNeverOverwrites(unittest.TestCase):
    """The success path must not destroy an existing archived record.

    Before the fix these called shutil.move, which REPLACES the destination on
    POSIX, so a repeated task id silently overwrote the earlier archive.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.tasks_dir = root / "archive-tasks"
        self.results_dir = root / "archive-results"
        self.live = root / "live"
        self.live.mkdir()
        self.month = datetime.now().strftime("%Y-%m")

    def _archive(self, src: Path, task_id: str) -> bool:
        return archive_file(src, "tasks", task_id, tasks_dir=self.tasks_dir,
                            results_dir=self.results_dir, log=lambda _m: None)

    def test_existing_archive_record_survives_a_repeated_id(self) -> None:
        (self.tasks_dir / self.month).mkdir(parents=True)
        prior = self.tasks_dir / self.month / "task-x.txt"
        prior.write_text("OLD-RECORD")
        src = self.live / "task-x.txt"
        src.write_text("NEW-RECORD")

        self.assertTrue(self._archive(src, "task-x"))
        self.assertEqual(prior.read_text(), "OLD-RECORD")
        self.assertFalse(src.exists(), "source must leave the live queue")
        self.assertEqual((self.tasks_dir / self.month / "task-x.txt.1").read_text(),
                         "NEW-RECORD")

    def test_normal_archive_still_lands_under_the_plain_name(self) -> None:
        src = self.live / "task-y.txt"
        src.write_text("BODY")
        self.assertTrue(self._archive(src, "task-y"))
        self.assertEqual((self.tasks_dir / self.month / "task-y.txt").read_text(),
                         "BODY")

    def test_cross_device_archive_copies_instead_of_linking(self) -> None:
        """os.link cannot span filesystems, and the archive can be a different
        mount. The O_EXCL copy fallback must preserve the bytes and the source."""
        import task_archive
        src = self.live / "task-c.txt"
        src.write_text("PAYLOAD")
        with mock.patch("os.link", side_effect=OSError(18, "Cross-device link")):
            self.assertTrue(self._archive(src, "task-c"))
        landed = self.tasks_dir / self.month / "task-c.txt"
        self.assertEqual(landed.read_text(), "PAYLOAD")
        self.assertFalse(src.exists(), "source must still leave the live queue")

    def test_cross_device_fallback_also_refuses_to_clobber(self) -> None:
        (self.tasks_dir / self.month).mkdir(parents=True)
        (self.tasks_dir / self.month / "task-d.txt").write_text("OLD")
        src = self.live / "task-d.txt"
        src.write_text("NEW")
        with mock.patch("os.link", side_effect=OSError(18, "Cross-device link")):
            self.assertTrue(self._archive(src, "task-d"))
        self.assertEqual((self.tasks_dir / self.month / "task-d.txt").read_text(), "OLD")
        self.assertEqual((self.tasks_dir / self.month / "task-d.txt.1").read_text(), "NEW")

    def test_a_failed_cross_device_copy_leaves_no_partial_and_keeps_the_source(self) -> None:
        """A half-written archive that reads as complete is worse than no archive."""
        src = self.live / "task-e.txt"
        src.write_text("PAYLOAD")
        with mock.patch("os.link", side_effect=OSError(18, "Cross-device link")), \
             mock.patch("shutil.copyfileobj", side_effect=OSError(28, "No space left")):
            self._archive(src, "task-e")
        self.assertFalse((self.tasks_dir / self.month / "task-e.txt").exists(),
                         "a partial copy must be removed, not left looking archived")
        self.assertTrue(src.exists() or
                        (self.live / "task-e.txt.archive-failed").exists(),
                        "the bytes must survive somewhere in the live queue")

    def test_quarantine_does_not_clobber_an_earlier_quarantine(self) -> None:
        self.tasks_dir.mkdir()
        os.chmod(self.tasks_dir, 0o500)
        self.addCleanup(os.chmod, self.tasks_dir, 0o700)
        (self.live / "task-z.txt.archive-failed").write_text("FIRST")
        src = self.live / "task-z.txt"
        src.write_text("SECOND")

        self.assertTrue(self._archive(src, "task-z"))
        self.assertEqual((self.live / "task-z.txt.archive-failed").read_text(), "FIRST")
        self.assertEqual((self.live / "task-z.txt.archive-failed.1").read_text(), "SECOND")
        self.assertFalse(src.exists())


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
