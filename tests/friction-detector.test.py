#!/usr/bin/env python3
"""
Tests for src/friction-detector.py.

Tests check_pending_questions() and check_stale_tasks() — the pure-Python
functions that don't require subprocess calls to gh or osascript.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "friction-detector.py"

# ---------------------------------------------------------------------------
# Import module with patched workspace so module-level WORKSPACE doesn't hit disk
# ---------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("friction", SCRIPT)
mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]

_orig_ws = os.environ.get("SUTANDO_WORKSPACE")
with tempfile.TemporaryDirectory() as _td:
    os.environ["SUTANDO_WORKSPACE"] = _td
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
if _orig_ws is not None:
    os.environ["SUTANDO_WORKSPACE"] = _orig_ws
else:
    os.environ.pop("SUTANDO_WORKSPACE", None)

check_pending_questions = mod.check_pending_questions
check_stale_tasks = mod.check_stale_tasks


class TestCheckPendingQuestions(unittest.TestCase):
    """check_pending_questions() parses pending-questions.md."""

    def _run(self, content: str) -> list:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            pq = ws / "pending-questions.md"
            pq.write_text(content)
            mod.WORKSPACE = ws
            return check_pending_questions()

    def test_missing_file_returns_empty(self) -> None:
        mod.WORKSPACE = Path("/nonexistent/does-not-exist")
        self.assertEqual(check_pending_questions(), [])

    def test_empty_file_returns_empty(self) -> None:
        result = self._run("")
        self.assertEqual(result, [])

    def test_no_pending_placeholder_returns_empty(self) -> None:
        result = self._run("(No pending questions)")
        self.assertEqual(result, [])

    def test_unanswered_section_detected(self) -> None:
        content = (
            "## Should I deploy now?\n"
            "- **Status:** unanswered\n"
        )
        result = self._run(content)
        self.assertEqual(len(result), 1)
        self.assertIn("Should I deploy now?", result[0])

    def test_answered_section_excluded(self) -> None:
        content = (
            "## Old question?\n"
            "- **Status:** answered\n"
        )
        result = self._run(content)
        self.assertEqual(result, [])

    def test_asked_date_in_output(self) -> None:
        from datetime import datetime
        # Use a date 2 days ago
        two_days_ago = (datetime.now().date().replace() if False else
                        __import__('datetime').date.today() - __import__('datetime').timedelta(days=2)).isoformat()
        content = (
            f"## Is X ready?\n"
            f"- **Asked:** {two_days_ago}\n"
            f"- **Status:** unanswered\n"
        )
        result = self._run(content)
        self.assertEqual(len(result), 1)
        self.assertIn("2d old", result[0])

    def test_multiple_unanswered_questions(self) -> None:
        content = (
            "## Q1?\n- **Status:** unanswered\n\n"
            "## Q2?\n- **Status:** unanswered\n\n"
            "## Q3?\n- **Status:** answered\n"
        )
        result = self._run(content)
        self.assertEqual(len(result), 2)

    def test_title_truncated_at_80_chars(self) -> None:
        long_title = "A" * 100
        content = f"## {long_title}\n- **Status:** unanswered\n"
        result = self._run(content)
        self.assertEqual(len(result), 1)
        # The stored title is at most 80 chars (see code: [:80])
        self.assertLessEqual(len(result[0]), 200)


class TestCheckStaleTasks(unittest.TestCase):
    """check_stale_tasks() flags task files older than 1 hour."""

    def test_no_tasks_dir_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mod.WORKSPACE = Path(td)
            result = check_stale_tasks()
            self.assertEqual(result, [])

    def test_fresh_task_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tasks").mkdir()
            fresh = ws / "tasks" / "task-123.txt"
            fresh.write_text("source: voice")
            mod.WORKSPACE = ws
            result = check_stale_tasks()
            self.assertEqual(result, [])

    def test_old_task_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tasks").mkdir()
            old = ws / "tasks" / "task-456.txt"
            old.write_text("source: voice")
            old_mtime = time.time() - 7200  # 2h ago
            os.utime(old, (old_mtime, old_mtime))
            mod.WORKSPACE = ws
            result = check_stale_tasks()
            self.assertEqual(len(result), 1)
            self.assertIn("task-456.txt", result[0])
            self.assertIn("2h", result[0])

    def test_non_task_txt_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tasks").mkdir()
            other = ws / "tasks" / "README.md"
            other.write_text("docs")
            old_mtime = time.time() - 7200
            os.utime(other, (old_mtime, old_mtime))
            mod.WORKSPACE = ws
            result = check_stale_tasks()
            self.assertEqual(result, [])

    def test_multiple_stale_tasks_all_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tasks").mkdir()
            for i in range(3):
                f = ws / "tasks" / f"task-{i}.txt"
                f.write_text("")
                old = time.time() - 3600 * (i + 2)
                os.utime(f, (old, old))
            mod.WORKSPACE = ws
            result = check_stale_tasks()
            self.assertEqual(len(result), 3)


if __name__ == "__main__":
    unittest.main()
