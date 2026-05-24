#!/usr/bin/env python3
"""
Tests for skills/macos-tools/scripts/calendar-reader.py

Coverage:
  read_events      — single event, missing-value location/notes,
                     all_day flag, sorted output, timeout, access-denied error,
                     generic error, returncode != 0, empty output, short line skipped
  format_for_humans — error passthrough, no events, multi-event, all-day label,
                      location appended

Run: python3 skills/macos-tools/tests/calendar_reader.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "calendar_reader",
    REPO / "skills" / "macos-tools" / "scripts" / "calendar-reader.py",
)
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)


def _fake_run(stdout="", returncode=0, stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _event_line(calendar="Work", title="Standup", start="Monday, March 16, 2026 at 9:00:00 AM",
                end="Monday, March 16, 2026 at 9:30:00 AM", location="", notes="", all_day="false"):
    return f"{calendar}|||{title}|||{start}|||{end}|||{location}|||{notes}|||{all_day}"


def _mock_subprocess(stdout="", returncode=0, stderr=""):
    return patch("subprocess.run", side_effect=[
        _fake_run("", 0),                         # open -ga Calendar
        _fake_run(stdout, returncode, stderr),    # osascript
    ])


# ── read_events ───────────────────────────────────────────────────────────────

class TestReadEvents(unittest.TestCase):
    def test_single_event_parsed(self):
        line = _event_line(title="Team meeting")
        with _mock_subprocess(line):
            data = cr.read_events()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["events"][0]["title"], "Team meeting")

    def test_missing_value_location_becomes_empty(self):
        line = _event_line(location="missing value")
        with _mock_subprocess(line):
            data = cr.read_events()
        self.assertEqual(data["events"][0]["location"], "")

    def test_missing_value_notes_becomes_empty(self):
        line = _event_line(notes="missing value")
        with _mock_subprocess(line):
            data = cr.read_events()
        self.assertEqual(data["events"][0]["notes"], "")

    def test_real_location_preserved(self):
        line = _event_line(location="Conference Room 4")
        with _mock_subprocess(line):
            data = cr.read_events()
        self.assertEqual(data["events"][0]["location"], "Conference Room 4")

    def test_all_day_true_parsed(self):
        line = _event_line(all_day="true")
        with _mock_subprocess(line):
            data = cr.read_events()
        self.assertTrue(data["events"][0]["all_day"])

    def test_all_day_false_parsed(self):
        line = _event_line(all_day="false")
        with _mock_subprocess(line):
            data = cr.read_events()
        self.assertFalse(data["events"][0]["all_day"])

    def test_events_sorted_by_start_across_days(self):
        # Sort is lexicographic on raw AppleScript date string.
        # Use different days to guarantee unambiguous order.
        lines = "\n".join([
            _event_line(title="Later", start="Sunday, March 17, 2026 at 9:00:00 AM"),
            _event_line(title="Earlier", start="Saturday, March 16, 2026 at 9:00:00 AM"),
        ])
        with _mock_subprocess(lines):
            data = cr.read_events()
        self.assertEqual(data["events"][0]["title"], "Earlier")
        self.assertEqual(data["events"][1]["title"], "Later")

    def test_empty_output_returns_zero_count(self):
        with _mock_subprocess(""):
            data = cr.read_events()
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["events"], [])

    def test_short_line_skipped(self):
        # Less than 4 parts — should be ignored
        with _mock_subprocess("Work|||Title|||Start"):
            data = cr.read_events()
        self.assertEqual(data["count"], 0)

    def test_timeout_returns_error(self):
        with patch("subprocess.run", side_effect=[
            _fake_run("", 0),
            subprocess.TimeoutExpired(["osascript"], 30),
        ]):
            data = cr.read_events()
        self.assertIn("error", data)
        self.assertIn("timed out", data["error"])

    def test_access_denied_error(self):
        with _mock_subprocess("", returncode=1, stderr="not allowed to access Calendar"):
            data = cr.read_events()
        self.assertIn("error", data)
        self.assertIn("denied", data["error"])

    def test_generic_applescript_error(self):
        with _mock_subprocess("", returncode=1, stderr="some script error"):
            data = cr.read_events()
        self.assertIn("error", data)
        self.assertEqual(data["count"], 0)

    def test_days_param_forwarded(self):
        line = _event_line(title="Event")
        with _mock_subprocess(line):
            data = cr.read_events(days=30)
        self.assertEqual(data["days"], 30)


# ── format_for_humans ─────────────────────────────────────────────────────────

class TestFormatForHumans(unittest.TestCase):
    def test_error_returned(self):
        result = cr.format_for_humans({"error": "Calendar access denied", "events": [], "count": 0})
        self.assertIn("Calendar error", result)

    def test_no_events_message(self):
        result = cr.format_for_humans({"events": [], "count": 0, "days": 7})
        self.assertIn("No events", result)

    def test_event_title_in_output(self):
        data = {
            "events": [{"calendar": "Work", "title": "Standup",
                         "start": "Mon at 9am", "end": "Mon at 9:30am",
                         "location": "", "notes": "", "all_day": False}],
            "count": 1, "days": 7,
        }
        result = cr.format_for_humans(data)
        self.assertIn("Standup", result)

    def test_location_appended(self):
        data = {
            "events": [{"calendar": "Work", "title": "Meeting",
                         "start": "Mon at 9am", "end": "Mon at 10am",
                         "location": "Conf Room", "notes": "", "all_day": False}],
            "count": 1, "days": 7,
        }
        result = cr.format_for_humans(data)
        self.assertIn("Conf Room", result)

    def test_all_day_label_added(self):
        data = {
            "events": [{"calendar": "Personal", "title": "Birthday",
                         "start": "Sunday, March 16, 2026 at 12:00:00 AM",
                         "end": "Sunday, March 16, 2026 at 11:59:00 PM",
                         "location": "", "notes": "", "all_day": True}],
            "count": 1, "days": 7,
        }
        result = cr.format_for_humans(data)
        self.assertIn("all day", result)


if __name__ == "__main__":
    unittest.main()
