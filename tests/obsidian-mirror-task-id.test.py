#!/usr/bin/env python3
r"""`_task_id_from_path` must yield the CANONICAL id for a claimed file.

Its old greedy `^task-(.+)\.txt$` returned `task-x.claimed-core-2`, so a claimed
task and its reply mirrored to two different notes.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from result_publish import publish_text  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "obsidian_mirror", REPO / "src" / "obsidian-mirror.py")
om = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(om)


class TaskIdFromPath(unittest.TestCase):
    def test_unpaired_result_cannot_create_a_completed_mirror(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = root / "results" / "task-proof.txt"
            result.parent.mkdir()
            result.write_text("raw answer")
            vault = root / "vault"
            notes = vault / "Sutando" / "Agent" / "Tasks"
            notes.mkdir(parents=True)
            self.assertFalse(om._write_result_mirror(vault, result))
            self.assertEqual(list(notes.iterdir()), [])
            publish_text(result, "verified answer")
            self.assertTrue(om._write_result_mirror(vault, result))
            self.assertIn("verified answer", (notes / "task-proof.md").read_text())

    def test_claimed_and_assigned_files_yield_the_canonical_id(self):
        for name in ("task-abc123.txt",
                     "task-abc123.claimed-core-2.txt",
                     "task-abc123.assigned-core-3.txt"):
            with self.subTest(name=name):
                self.assertEqual(om._task_id_from_path(Path(name)), "task-abc123")

    def test_the_regression_a_greedy_pattern_gets_wrong(self):
        got = om._task_id_from_path(Path("task-abc123.claimed-core-2.txt"))
        self.assertNotEqual(got, "task-abc123.claimed-core-2")
        self.assertEqual(got, "task-abc123")

    def test_a_non_task_file_is_rejected_not_guessed(self):
        self.assertIsNone(om._task_id_from_path(Path("notes.txt")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
