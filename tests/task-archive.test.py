"""Tests for src/task_archive.py — find_task_file() helper (closes #933)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from task_archive import find_task_file


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
