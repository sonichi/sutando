#!/usr/bin/env python3
"""Tests for check_notes_split_brain() in src/health-check.py (PR #1288 / #1266).

Run: python3 tests/health-check-notes-split-brain.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

_MEMORY_ENV_KEYS = ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Load health-check.py as a module (hyphenated name prevents normal import).
spec = importlib.util.spec_from_file_location(
    "health_check", REPO / "src" / "health-check.py"
)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class TestNotesSplitBrain(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hc-notes-sb-"))
        # Stash the module-level globals so we can restore them.
        self._saved_repo = hc.REPO_DIR
        self._saved_ws = hc.WORKSPACE_DIR
        # Clear memory-dir env vars so shared_personal_path falls back to
        # WORKSPACE_DIR instead of the real on-disk memory dir.
        self._saved_mem_env = {k: os.environ.pop(k, None) for k in _MEMORY_ENV_KEYS}

    def tearDown(self):
        hc.REPO_DIR = self._saved_repo
        hc.WORKSPACE_DIR = self._saved_ws
        for k, v in self._saved_mem_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _setup_dirs(self, repo_files=None, ws_files=None, same_path=False):
        repo_notes = self.tmp / "repo" / "notes"
        repo_notes.mkdir(parents=True, exist_ok=True)
        if same_path:
            ws_notes = repo_notes
        else:
            ws_notes = self.tmp / "workspace" / "notes"
            ws_notes.mkdir(parents=True, exist_ok=True)

        for name in (repo_files or []):
            (repo_notes / name).write_text("# test")
        for name in (ws_files or []):
            (ws_notes / name).write_text("# test")

        hc.REPO_DIR = repo_notes.parent
        hc.WORKSPACE_DIR = ws_notes.parent
        return repo_notes, ws_notes

    def test_same_path_returns_none(self):
        """No false alarm when repo and workspace point at the same notes dir."""
        self._setup_dirs(
            repo_files=["todo.md", "meeting.md"],
            same_path=True,
        )
        self.assertIsNone(hc.check_notes_split_brain())

    def test_workspace_notes_absent_returns_none(self):
        """Returns None when workspace notes/ dir doesn't exist yet."""
        repo_notes = self.tmp / "repo" / "notes"
        repo_notes.mkdir(parents=True)
        (repo_notes / "todo.md").write_text("# test")

        hc.REPO_DIR = repo_notes.parent
        hc.WORKSPACE_DIR = self.tmp / "workspace"  # no notes/ subdir

        self.assertIsNone(hc.check_notes_split_brain())

    def test_repo_notes_absent_returns_none(self):
        """Returns None when repo notes/ dir doesn't exist."""
        ws_notes = self.tmp / "workspace" / "notes"
        ws_notes.mkdir(parents=True)
        (ws_notes / "todo.md").write_text("# test")

        hc.REPO_DIR = self.tmp / "repo"  # no notes/ subdir
        hc.WORKSPACE_DIR = ws_notes.parent

        self.assertIsNone(hc.check_notes_split_brain())

    def test_no_overlap_returns_none(self):
        """Two separate notes dirs with distinct filenames → no warning."""
        self._setup_dirs(
            repo_files=["a.md", "b.md"],
            ws_files=["c.md", "d.md"],
        )
        self.assertIsNone(hc.check_notes_split_brain())

    def test_overlap_returns_warning(self):
        """Overlapping filenames produce a 'warn' status dict."""
        self._setup_dirs(
            repo_files=["todo.md", "meeting.md"],
            ws_files=["todo.md", "other.md"],
        )
        result = hc.check_notes_split_brain()
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "notes-split-brain")
        self.assertEqual(result["status"], "warn")
        self.assertIn("todo.md", result["detail"])

    def test_overlap_count_in_detail(self):
        """Detail mentions the number of overlapping files."""
        self._setup_dirs(
            repo_files=["a.md", "b.md", "c.md"],
            ws_files=["a.md", "b.md", "c.md"],
        )
        result = hc.check_notes_split_brain()
        self.assertIsNotNone(result)
        self.assertIn("3", result["detail"])

    def test_more_than_three_overlap_shows_tail(self):
        """With 4+ overlapping files the detail includes '… and N more'."""
        names = [f"note{i}.md" for i in range(5)]
        self._setup_dirs(repo_files=names, ws_files=names)
        result = hc.check_notes_split_brain()
        self.assertIsNotNone(result)
        # Only 3 examples shown, remainder in tail.
        self.assertIn("more", result["detail"])

    def test_detail_mentions_migrate_script(self):
        """Warning directs the user to sutando-migrate.sh."""
        self._setup_dirs(
            repo_files=["todo.md"],
            ws_files=["todo.md"],
        )
        result = hc.check_notes_split_brain()
        self.assertIsNotNone(result)
        self.assertIn("sutando-migrate.sh", result["detail"])


if __name__ == "__main__":
    unittest.main()
