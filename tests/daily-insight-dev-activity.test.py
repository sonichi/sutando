#!/usr/bin/env python3
"""Tests for daily-insight.py dev-activity signal.

2026-07-21: the owner pushed back that the daily insight reduced his day to
"you've created 7 notes" — a shallow workspace-folder metric blind to the real
work he did (shipping commits, PRs, meetings). Fix: surface git commit activity
in the last 24h as the highest-priority insight, so a productive day doesn't
read as "just notes."

All git calls are mocked — no real repo history is inspected here.
"""
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "daily-insight.py"


def _load():
    spec = importlib.util.spec_from_file_location("daily_insight", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(stdout, returncode=0):
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=""
    )


# Two commits touching src/ and tests/, one touching a top-level file.
GIT_OUT = (
    "C:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    "src/morning-briefing.py\n"
    "tests/foo.test.py\n"
    "C:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
    "src/daily-insight.py\n"
    "README.md\n"
)


class TestAnalyzeDevActivity(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_counts_commits_and_dirs(self):
        with patch.object(self.mod.subprocess, "run", return_value=_git(GIT_OUT)):
            dev = self.mod.analyze_dev_activity()
        self.assertEqual(dev["commits_24h"], 2)
        top = dict(dev["top_dirs"])
        self.assertEqual(top.get("src"), 2)
        self.assertEqual(top.get("tests"), 1)
        # A top-level file (README.md, no "/") is not counted as a dir.
        self.assertNotIn("README.md", top)

    def test_no_commits_returns_none(self):
        with patch.object(self.mod.subprocess, "run", return_value=_git("")):
            self.assertIsNone(self.mod.analyze_dev_activity())

    def test_git_failure_returns_none(self):
        with patch.object(self.mod.subprocess, "run", return_value=_git("", returncode=128)):
            self.assertIsNone(self.mod.analyze_dev_activity())

    def test_git_missing_returns_none(self):
        with patch.object(self.mod.subprocess, "run", side_effect=OSError("no git")):
            self.assertIsNone(self.mod.analyze_dev_activity())

    def test_timeout_returns_none(self):
        with patch.object(
            self.mod.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10),
        ):
            self.assertIsNone(self.mod.analyze_dev_activity())


class TestInsightPriority(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_dev_activity_wins_over_notes(self):
        """When commits exist, the insight is the commit headline, not notes."""
        with patch.object(self.mod.subprocess, "run", return_value=_git(GIT_OUT)):
            insight = self.mod.generate_insight()
        self.assertIn("commit", insight)
        self.assertIn("last 24h", insight)
        self.assertNotIn("notes", insight.lower())

    def test_falls_back_when_no_commits(self):
        """No commits → dev signal absent → existing note/call/task logic runs."""
        with patch.object(self.mod, "analyze_dev_activity", return_value=None), \
             patch.object(self.mod, "load_calls", return_value=[]), \
             patch.object(self.mod, "analyze_note_activity",
                          return_value={"total": 20, "recent_7d": 7,
                                        "top_tags": [("learned", 3), ("codex", 2)]}), \
             patch.object(self.mod, "analyze_task_patterns", return_value=self.mod.Counter()):
            insight = self.mod.generate_insight()
        self.assertIn("notes", insight.lower())

    def test_singular_commit_grammar(self):
        one = "C:cccccccccccccccccccccccccccccccccccccccc\nsrc/x.py\n"
        with patch.object(self.mod.subprocess, "run", return_value=_git(one)):
            insight = self.mod.generate_insight()
        self.assertIn("1 commit in the last 24h", insight)
        self.assertNotIn("1 commits", insight)


if __name__ == "__main__":
    import os
    _r = unittest.main(exit=False)
    # Flush coverage BEFORE os._exit — os._exit skips coverage.py's atexit
    # writer, so without this the coverage gate sees zero data for this file
    # and reds diff-cover on the daily-insight.py changes it exercises.
    try:
        import coverage
        _cov = coverage.Coverage.current()
        if _cov is not None:
            _cov.save()
    except Exception:
        pass
    os._exit(0 if _r.result.wasSuccessful() else 1)
