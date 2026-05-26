#!/usr/bin/env python3
"""
Tests for src/check-pending-questions.py.

Tests the pure logic functions that don't touch external processes
(presenter_mode_active, get_waiting_questions, should_notify).
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "check-pending-questions.py"

# ---------------------------------------------------------------------------
# Import helper functions from the module
# ---------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("cpq", SCRIPT)
cpq_mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]

# Patch workspace resolution so the module-level globals don't touch disk
import os
_orig_env = os.environ.get("SUTANDO_WORKSPACE")
with tempfile.TemporaryDirectory() as _td:
    os.environ["SUTANDO_WORKSPACE"] = _td
    spec.loader.exec_module(cpq_mod)  # type: ignore[union-attr]
if _orig_env is not None:
    os.environ["SUTANDO_WORKSPACE"] = _orig_env
else:
    os.environ.pop("SUTANDO_WORKSPACE", None)

presenter_mode_active = cpq_mod.presenter_mode_active
get_waiting_questions = cpq_mod.get_waiting_questions
should_notify = cpq_mod.should_notify


class TestPresenterModeActive(unittest.TestCase):
    """presenter_mode_active() reads and validates the sentinel file."""

    def _sentinel(self, td: Path) -> Path:
        sentinel = td / "state" / "presenter-mode.sentinel"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        return sentinel

    def test_no_sentinel_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cpq_mod.PRESENTER_SENTINEL = Path(td) / "state" / "presenter-mode.sentinel"
            self.assertFalse(presenter_mode_active())

    def test_future_expiry_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            s = self._sentinel(Path(td))
            future = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 3600))
            s.write_text(future)
            cpq_mod.PRESENTER_SENTINEL = s
            self.assertTrue(presenter_mode_active())

    def test_past_expiry_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            s = self._sentinel(Path(td))
            past = "2020-01-01T00:00:00Z"
            s.write_text(past)
            cpq_mod.PRESENTER_SENTINEL = s
            self.assertFalse(presenter_mode_active())

    def test_malformed_content_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            s = self._sentinel(Path(td))
            s.write_text("garbage not a date")
            cpq_mod.PRESENTER_SENTINEL = s
            self.assertFalse(presenter_mode_active())

    def test_empty_sentinel_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            s = self._sentinel(Path(td))
            s.write_text("")
            cpq_mod.PRESENTER_SENTINEL = s
            self.assertFalse(presenter_mode_active())


class TestGetWaitingQuestions(unittest.TestCase):
    """get_waiting_questions() parses pending-questions.md."""

    def _pq(self, content: str) -> list:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            path = Path(f.name)
        cpq_mod.PQ_FILE = path
        result = get_waiting_questions()
        path.unlink()
        return result

    def test_missing_file_returns_empty(self) -> None:
        cpq_mod.PQ_FILE = Path("/nonexistent/pending-questions.md")
        self.assertEqual(get_waiting_questions(), [])

    def test_empty_file_returns_empty(self) -> None:
        result = self._pq("")
        self.assertEqual(result, [])

    def test_unanswered_question_detected(self) -> None:
        content = "## Should I do X?\n\n**Status:** unanswered\n\nSome details.\n"
        result = self._pq(content)
        self.assertEqual(len(result), 1)
        self.assertIn("Should I do X?", result[0]["title"])

    def test_waiting_status_detected(self) -> None:
        content = "## Deploy now?\n\n**Status:** Waiting for Bassil\n"
        result = self._pq(content)
        self.assertEqual(len(result), 1)

    def test_answered_question_excluded(self) -> None:
        content = "## Old question?\n\n**Status:** answered\n\n## New question?\n\n**Status:** unanswered\n"
        result = self._pq(content)
        self.assertEqual(len(result), 1)
        self.assertIn("New question?", result[0]["title"])

    def test_section_without_status_excluded(self) -> None:
        content = "## No status section\n\nJust some text here.\n"
        result = self._pq(content)
        self.assertEqual(result, [])

    def test_multiple_waiting_questions(self) -> None:
        content = (
            "## Q1?\n\n**Status:** unanswered\n\n"
            "## Q2?\n\n**Status:** unanswered\n"
        )
        result = self._pq(content)
        self.assertEqual(len(result), 2)


class TestShouldNotify(unittest.TestCase):
    """should_notify() enforces 1-hour cooldown via mtime."""

    def test_no_sentinel_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cpq_mod.LAST_NOTIFY_FILE = Path(td) / ".last-pq-notify"
            self.assertTrue(should_notify())

    def test_fresh_sentinel_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / ".last-pq-notify"
            sentinel.write_text("x")
            cpq_mod.LAST_NOTIFY_FILE = sentinel
            self.assertFalse(should_notify())

    def test_old_sentinel_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / ".last-pq-notify"
            sentinel.write_text("x")
            old = time.time() - 7200  # 2h ago
            import os
            os.utime(sentinel, (old, old))
            cpq_mod.LAST_NOTIFY_FILE = sentinel
            self.assertTrue(should_notify())


if __name__ == "__main__":
    unittest.main()
