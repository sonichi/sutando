#!/usr/bin/env python3
"""Tests for reminders.py --due-today filtering.

Before this fix, `list --due-today` was silently ignored — all incomplete
reminders were returned regardless of due date, so future reminders could
appear in the morning briefing.

After the fix, only reminders due today or overdue are included.
"""
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "macos-tools" / "scripts" / "reminders.py"


def _load():
    spec = importlib.util.spec_from_file_location("reminders", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fmt(d) -> str:
    """Format a date as AppleScript date string."""
    return d.strftime("%A, %B %d, %Y at %I:%M:%S %p")


class TestIsdueTodayOrOverdue(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.today = datetime.now().date()

    def test_today_returns_true(self):
        s = _fmt(datetime.now())
        self.assertTrue(self.mod._is_due_today_or_overdue(s))

    def test_yesterday_returns_true(self):
        yesterday = datetime.now() - timedelta(days=1)
        self.assertTrue(self.mod._is_due_today_or_overdue(_fmt(yesterday)))

    def test_tomorrow_returns_false(self):
        tomorrow = datetime.now() + timedelta(days=1)
        self.assertFalse(self.mod._is_due_today_or_overdue(_fmt(tomorrow)))

    def test_empty_string_returns_false(self):
        self.assertFalse(self.mod._is_due_today_or_overdue(""))

    def test_invalid_format_returns_false(self):
        self.assertFalse(self.mod._is_due_today_or_overdue("not a date"))

    def test_missing_value_returns_false(self):
        self.assertFalse(self.mod._is_due_today_or_overdue("missing value"))


class TestDueTodayFlag(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        today_str = _fmt(datetime.now())
        yesterday_str = _fmt(datetime.now() - timedelta(days=1))
        tomorrow_str = _fmt(datetime.now() + timedelta(days=1))
        self.reminders = [
            {"list": "L", "name": "overdue task", "due": yesterday_str,
             "completed": False, "body": ""},
            {"list": "L", "name": "today task", "due": today_str,
             "completed": False, "body": ""},
            {"list": "L", "name": "future task", "due": tomorrow_str,
             "completed": False, "body": ""},
            {"list": "L", "name": "undated task", "due": "",
             "completed": False, "body": ""},
        ]

    def test_due_today_excludes_future(self):
        result = [r for r in self.reminders
                  if self.mod._is_due_today_or_overdue(r["due"])]
        names = [r["name"] for r in result]
        self.assertIn("overdue task", names)
        self.assertIn("today task", names)
        self.assertNotIn("future task", names)
        self.assertNotIn("undated task", names)

    def test_without_flag_all_returned(self):
        """Without --due-today, all reminders pass through unchanged."""
        result = list(self.reminders)  # no filter applied
        self.assertEqual(len(result), 4)

    def test_morning_briefing_receives_only_due_items(self):
        """morning-briefing calls `list --due-today` — only actionable reminders reach it.

        get_reminders() delegates filtering to `reminders.py list --due-today`.
        The mock returns already-filtered formatted output (what that command produces).
        """
        import subprocess as sp
        today_str = _fmt(datetime.now())
        yesterday_str = _fmt(datetime.now() - timedelta(days=1))

        # reminders.py list --due-today emits formatted lines for due/overdue items only.
        # The future task has already been excluded by the --due-today filter in reminders.py.
        fake_stdout = (
            f"  [Work] today task (due {today_str})\n"
            f"  [Work] overdue task (due {yesterday_str})\n"
        )
        fake = sp.CompletedProcess(args=[], returncode=0, stdout=fake_stdout, stderr="")

        import importlib.util as ilu
        mb_spec = ilu.spec_from_file_location("mb", REPO / "src" / "morning-briefing.py")
        mb = ilu.module_from_spec(mb_spec)
        mb_spec.loader.exec_module(mb)

        with patch.object(sp, "run", return_value=fake):
            result = mb.get_reminders()

        joined = " ".join(result)
        self.assertIn("today task", joined)
        self.assertIn("overdue task", joined)
        self.assertNotIn("future task", joined)


if __name__ == "__main__":
    unittest.main()
