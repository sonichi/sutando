#!/usr/bin/env python3
"""
Tests for skills/macos-tools/scripts/reminders.py

Coverage:
  list_reminders   — single reminder, multi-reminders, missing value due/body,
                     completed flag, error response, empty output
  add_reminder     — no due, with due, with list_name, error passthrough
  complete_reminder — success, not found, error passthrough
  list_reminder_lists — parses comma-separated names, error returns empty

Run: python3 skills/macos-tools/tests/reminders.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "reminders",
    REPO / "skills" / "macos-tools" / "scripts" / "reminders.py",
)
rm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rm)


def _mock_applescript(stdout="", stderr=""):
    """Patch run_applescript to return (stdout, stderr)."""
    return patch.object(rm, "run_applescript", return_value=(stdout, stderr))


# ── list_reminder_lists ───────────────────────────────────────────────────────

class TestListReminderLists(unittest.TestCase):
    def test_parses_comma_separated(self):
        with _mock_applescript("Work, Personal, Groceries"):
            result = rm.list_reminder_lists()
        self.assertEqual(result, ["Work", "Personal", "Groceries"])

    def test_error_returns_empty(self):
        with _mock_applescript("", stderr="Reminders not found"):
            result = rm.list_reminder_lists()
        self.assertEqual(result, [])

    def test_single_list(self):
        with _mock_applescript("Reminders"):
            result = rm.list_reminder_lists()
        self.assertEqual(result, ["Reminders"])

    def test_strips_whitespace(self):
        with _mock_applescript("  Work  ,  Personal  "):
            result = rm.list_reminder_lists()
        self.assertEqual(result, ["Work", "Personal"])


# ── list_reminders ────────────────────────────────────────────────────────────

class TestListReminders(unittest.TestCase):
    def _reminder_line(self, lst="Work", name="Buy groceries", due="",
                       completed="false", body=""):
        return f"{lst}|||{name}|||{due}|||{completed}|||{body}"

    def test_single_reminder(self):
        line = self._reminder_line(name="Call Bob")
        with _mock_applescript(line):
            result = rm.list_reminders()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Call Bob")
        self.assertEqual(result[0]["list"], "Work")

    def test_completed_false_parsed(self):
        line = self._reminder_line(completed="false")
        with _mock_applescript(line):
            result = rm.list_reminders()
        self.assertFalse(result[0]["completed"])

    def test_completed_true_parsed(self):
        line = self._reminder_line(completed="true")
        with _mock_applescript(line):
            result = rm.list_reminders()
        self.assertTrue(result[0]["completed"])

    def test_missing_value_due_becomes_empty(self):
        line = self._reminder_line(due="missing value")
        with _mock_applescript(line):
            result = rm.list_reminders()
        self.assertEqual(result[0]["due"], "")

    def test_real_due_date_preserved(self):
        line = self._reminder_line(due="Monday, March 17, 2026 at 9:00:00 AM")
        with _mock_applescript(line):
            result = rm.list_reminders()
        self.assertIn("March", result[0]["due"])

    def test_missing_value_body_becomes_empty(self):
        line = self._reminder_line(body="missing value")
        with _mock_applescript(line):
            result = rm.list_reminders()
        self.assertEqual(result[0]["body"], "")

    def test_empty_output_returns_empty_list(self):
        with _mock_applescript(""):
            result = rm.list_reminders()
        self.assertEqual(result, [])

    def test_error_returns_error_dict(self):
        with _mock_applescript("", stderr="Reminders is not running"):
            result = rm.list_reminders()
        self.assertEqual(len(result), 1)
        self.assertIn("error", result[0])

    def test_multiple_reminders(self):
        lines = "\n".join([
            self._reminder_line(name="First"),
            self._reminder_line(name="Second"),
        ])
        with _mock_applescript(lines):
            result = rm.list_reminders()
        self.assertEqual(len(result), 2)
        names = [r["name"] for r in result]
        self.assertIn("First", names)
        self.assertIn("Second", names)

    def test_short_line_skipped(self):
        # A line with fewer than 4 parts should be ignored
        with _mock_applescript("Work|||name|||"):
            result = rm.list_reminders()
        self.assertEqual(result, [])


# ── add_reminder ──────────────────────────────────────────────────────────────

class TestAddReminder(unittest.TestCase):
    def test_returns_added_message(self):
        with _mock_applescript("reminder id 1"):
            result = rm.add_reminder("Buy milk")
        self.assertIn("Added", result)
        self.assertIn("Buy milk", result)

    def test_with_due_date_in_result(self):
        with _mock_applescript("ok"):
            result = rm.add_reminder("Meeting", due_date="2026-03-17")
        self.assertIn("due", result)
        self.assertIn("2026-03-17", result)

    def test_no_due_date_no_parenthesis(self):
        with _mock_applescript("ok"):
            result = rm.add_reminder("Task")
        self.assertNotIn("due", result)

    def test_error_passthrough(self):
        with _mock_applescript("", stderr="Reminders not accessible"):
            result = rm.add_reminder("Fail")
        self.assertIn("Error", result)


# ── complete_reminder ─────────────────────────────────────────────────────────

class TestCompleteReminder(unittest.TestCase):
    def test_success_returns_done(self):
        with _mock_applescript("Done: Buy milk"):
            result = rm.complete_reminder("Buy milk")
        self.assertIn("Done", result)

    def test_not_found(self):
        with _mock_applescript("Not found: Ghost task"):
            result = rm.complete_reminder("Ghost task")
        self.assertIn("Not found", result)

    def test_error_passthrough(self):
        with _mock_applescript("", stderr="AppleScript error"):
            result = rm.complete_reminder("Anything")
        self.assertIn("Error", result)


if __name__ == "__main__":
    unittest.main()
