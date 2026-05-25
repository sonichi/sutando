#!/usr/bin/env python3
"""Tests for skills/proactive-notify/scripts/sources/google_calendar.py.

Covers:
  - _owner_calendar_set() config parsing
  - owner_calendars filter in fetch() (#964)
  - untitled event skip (#967)
  - cross-calendar dedup (#966)
"""
import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
MOD_PATH = REPO / "skills" / "proactive-notify" / "scripts" / "sources" / "google_calendar.py"

spec = importlib.util.spec_from_file_location("google_calendar", MOD_PATH)
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)


def _make_event(title: str, minutes_from_now: int = 10, cal: str = "primary") -> dict:
    """Build a minimal raw gws event dict."""
    start = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return {
        "summary": title,
        "start": start.isoformat(),
        "calendar": cal,
    }


class TestOwnerCalendarSet(unittest.TestCase):
    def test_none_when_not_configured(self):
        self.assertIsNone(gc._owner_calendar_set({}))

    def test_none_on_empty_list(self):
        self.assertIsNone(gc._owner_calendar_set({"owner_calendars": []}))

    def test_returns_set_from_list(self):
        result = gc._owner_calendar_set({"owner_calendars": ["khilo@live.ca", "primary"]})
        self.assertIsInstance(result, set)
        self.assertIn("khilo@live.ca", result)
        self.assertIn("primary", result)

    def test_case_insensitive_normalization(self):
        result = gc._owner_calendar_set({"owner_calendars": ["Bassil Khilo"]})
        self.assertIn("bassil khilo", result)
        self.assertNotIn("Bassil Khilo", result)

    def test_strips_whitespace(self):
        result = gc._owner_calendar_set({"owner_calendars": ["  khilo@live.ca  "]})
        self.assertIn("khilo@live.ca", result)


class TestFetchOwnerCalendarFilter(unittest.TestCase):
    """issue #964 — events from other people's calendars must be filtered out."""

    def _fetch_with_events(self, events: list[dict], config: dict) -> list[dict]:
        with patch.object(gc, "_iter_raw_events", return_value=iter(events)):
            return gc.fetch(config)

    def test_includes_event_from_owner_calendar(self):
        events = [_make_event("Standup", cal="khilo@live.ca")]
        config = {"lookahead_min": 15, "owner_calendars": ["khilo@live.ca"]}
        result = self._fetch_with_events(events, config)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Standup")

    def test_excludes_event_from_other_person_calendar(self):
        events = [_make_event("Friend's Wedding", cal="mark@ag2.ai")]
        config = {"lookahead_min": 15, "owner_calendars": ["khilo@live.ca"]}
        result = self._fetch_with_events(events, config)
        self.assertEqual(len(result), 0)

    def test_no_filter_when_owner_calendars_absent(self):
        events = [
            _make_event("My Meeting", cal="khilo@live.ca"),
            _make_event("Their Event", cal="other@example.com"),
        ]
        config = {"lookahead_min": 15}  # no owner_calendars key
        result = self._fetch_with_events(events, config)
        self.assertEqual(len(result), 2)

    def test_filter_is_case_insensitive(self):
        events = [_make_event("Standup", cal="KHILO@LIVE.CA")]
        config = {"lookahead_min": 15, "owner_calendars": ["khilo@live.ca"]}
        result = self._fetch_with_events(events, config)
        self.assertEqual(len(result), 1)

    def test_multiple_owner_calendars_allowed(self):
        events = [
            _make_event("Event A", cal="khilo@live.ca"),
            _make_event("Event B", cal="Bassil Khilo"),
            _make_event("Event C", cal="mark@ag2.ai"),
        ]
        config = {"lookahead_min": 15, "owner_calendars": ["khilo@live.ca", "Bassil Khilo"]}
        result = self._fetch_with_events(events, config)
        self.assertEqual(len(result), 2)
        titles = {r["title"] for r in result}
        self.assertIn("Event A", titles)
        self.assertIn("Event B", titles)


class TestFetchUntitledSkip(unittest.TestCase):
    """issue #967 — untitled events must be silently skipped."""

    def _fetch_with_events(self, events: list[dict], config: dict) -> list[dict]:
        with patch.object(gc, "_iter_raw_events", return_value=iter(events)):
            return gc.fetch(config)

    def test_skips_empty_summary(self):
        events = [_make_event("")]
        events[0]["summary"] = ""
        result = self._fetch_with_events(events, {"lookahead_min": 15})
        self.assertEqual(len(result), 0)

    def test_skips_whitespace_only_summary(self):
        events = [_make_event("   ")]
        events[0]["summary"] = "   "
        result = self._fetch_with_events(events, {"lookahead_min": 15})
        self.assertEqual(len(result), 0)

    def test_keeps_event_with_title(self):
        events = [_make_event("Standup")]
        result = self._fetch_with_events(events, {"lookahead_min": 15})
        self.assertEqual(len(result), 1)


class TestFetchDedup(unittest.TestCase):
    """issue #966 — same event on multiple subscribed calendars appears once."""

    def _fetch_with_events(self, events: list[dict], config: dict) -> list[dict]:
        with patch.object(gc, "_iter_raw_events", return_value=iter(events)):
            return gc.fetch(config)

    def test_dedup_same_event_two_calendars(self):
        e1 = _make_event("Standup", cal="khilo@live.ca")
        e2 = dict(e1)  # same title + start_iso
        e2["calendar"] = "team@ag2.ai"
        result = self._fetch_with_events([e1, e2], {"lookahead_min": 15})
        self.assertEqual(len(result), 1)

    def test_different_titles_not_deduped(self):
        e1 = _make_event("Standup", cal="khilo@live.ca")
        e2 = _make_event("1:1", cal="team@ag2.ai")
        result = self._fetch_with_events([e1, e2], {"lookahead_min": 15})
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestOwnerCalendarSet, TestFetchOwnerCalendarFilter,
        TestFetchUntitledSkip, TestFetchDedup,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
