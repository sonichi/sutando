#!/usr/bin/env python3
"""Structural tests for context-drop source field fix (issue #969).

Guards:
  1. writeTask in main.swift emits source: hotkey + access_tier: owner
  2. refreshContextualChips archives stale hotkey results (not other sources)
  3. archive path uses today's date stamp
  4. task file is read to check source before archiving
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SWIFT = (REPO / "src" / "Sutando" / "main.swift").read_text()


class TestWriteTaskSourceField(unittest.TestCase):
    def _write_task_block(self) -> str:
        idx = SWIFT.find("func writeTask(")
        end = SWIFT.find("\n    func ", idx + 1)
        return SWIFT[idx:end] if end != -1 else SWIFT[idx:idx + 600]

    def test_source_hotkey_present(self):
        block = self._write_task_block()
        self.assertIn("source: hotkey", block)

    def test_access_tier_owner_present(self):
        block = self._write_task_block()
        self.assertIn("access_tier: owner", block)

    def test_source_and_tier_before_task_line(self):
        block = self._write_task_block()
        src_idx = block.find("source: hotkey")
        tier_idx = block.find("access_tier: owner")
        task_idx = block.find("task: User dropped")
        self.assertGreater(task_idx, src_idx, "source: must appear before task: line")
        self.assertGreater(task_idx, tier_idx, "access_tier: must appear before task: line")


class TestRefreshContextualChipsArchiving(unittest.TestCase):
    def _chips_block(self) -> str:
        idx = SWIFT.find("func refreshContextualChips(")
        end = SWIFT.find("\n    func ", idx + 1)
        return SWIFT[idx:end] if end != -1 else SWIFT[idx:idx + 3000]

    def test_archive_dir_created_for_hotkey_results(self):
        block = self._chips_block()
        self.assertIn("archive-", block, "archive dir must reference 'archive-' prefix")

    def test_checks_source_hotkey_before_archiving(self):
        block = self._chips_block()
        self.assertIn("source: hotkey", block)

    def test_reads_task_file_to_determine_source(self):
        block = self._chips_block()
        self.assertIn("tasksDir", block)
        self.assertIn("taskPath", block)
        self.assertIn("taskBody", block)

    def test_moves_stale_files_past_600s_window(self):
        block = self._chips_block()
        self.assertIn("600", block)
        self.assertIn("moveItem", block)

    def test_creates_archive_directory(self):
        block = self._chips_block()
        self.assertIn("createDirectory", block)

    def test_chip_still_shown_for_recent_results(self):
        block = self._chips_block()
        # The chip-append path must still exist
        self.assertIn('chips.append(["label": "Recent result"', block)


if __name__ == "__main__":
    unittest.main()
