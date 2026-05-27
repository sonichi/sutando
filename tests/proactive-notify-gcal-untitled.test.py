#!/usr/bin/env python3
"""
Regression guard: proactive-notify google_calendar source must skip untitled events.

Background (sonichi/sutando#967): `gws calendar +agenda` returns events with no
`summary` field (null or missing). Before the fix, these fell through as "(untitled)"
and produced notifications like "Meeting in 11 min: (untitled) at 11:00 PDT" — unhelpful.
The fix skips them entirely (v1). Attendee-name rendering is planned for v2 when the
source adapter is enriched to pull attendees.

Guards:
  1. Events with no `summary` field are skipped
  2. Events with `summary: null` are skipped
  3. Events with `summary: ""` are skipped
  4. Events with a real title are NOT skipped
  5. Source does NOT contain `"(untitled)"` fallback string
  6. Structural: skip-untitled comment documents the v2 upgrade path

Run: python3 tests/proactive-notify-gcal-untitled.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = ROOT / "skills" / "proactive-notify" / "scripts"
GCAL_SRC = ROOT / "skills" / "proactive-notify" / "scripts" / "sources" / "google_calendar.py"
sys.path.insert(0, str(SKILL_SCRIPTS))

from sources.google_calendar import fetch  # noqa: E402

SOURCE = GCAL_SRC.read_text(encoding="utf-8")


def _make_raw_event(summary, minutes_from_now=5):
    """Build a minimal raw event dict as gws would return."""
    start_dt = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return {
        "summary": summary,
        "start": start_dt.isoformat(),
        "calendar": "primary",
        "location": "",
    }


class TestUntitledEventSkip(unittest.TestCase):
    def _fetch_with_events(self, raw_events):
        with patch("sources.google_calendar._iter_raw_events", return_value=iter(raw_events)):
            return fetch({"lookahead_min": 15})

    def test_event_with_no_summary_field_is_skipped(self):
        """An event dict without a 'summary' key must be skipped."""
        raw = _make_raw_event(summary=None)
        del raw["summary"]  # missing key, not None
        events = self._fetch_with_events([raw])
        self.assertEqual(events, [], "Event with no 'summary' key must be excluded")

    def test_event_with_null_summary_is_skipped(self):
        """An event with `summary: None` must be skipped."""
        raw = _make_raw_event(summary=None)
        events = self._fetch_with_events([raw])
        self.assertEqual(events, [], "Event with null summary must be excluded")

    def test_event_with_empty_string_summary_is_skipped(self):
        """An event with `summary: ""` must be skipped."""
        raw = _make_raw_event(summary="")
        events = self._fetch_with_events([raw])
        self.assertEqual(events, [], 'Event with empty string summary must be excluded')

    def test_event_with_real_title_is_included(self):
        """A normal titled event must pass through."""
        raw = _make_raw_event(summary="Team standup")
        events = self._fetch_with_events([raw])
        self.assertEqual(len(events), 1, "Titled event must be included")
        self.assertEqual(events[0]["title"], "Team standup")

    def test_source_does_not_contain_untitled_fallback(self):
        """The (untitled) fallback string must no longer appear in the source."""
        self.assertNotIn('"(untitled)"', SOURCE, (
            'The "(untitled)" fallback must be removed — untitled events should be skipped, '
            "not forwarded with a placeholder title. See sonichi/sutando#967."
        ))

    def test_skip_untitled_comment_documents_v2_path(self):
        """The skip comment must reference the v2 upgrade path."""
        self.assertIn("v2", SOURCE.lower(), (
            "Skip-untitled comment must mention the v2 attendee-name rendering plan."
        ))


def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestUntitledEventSkip)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
