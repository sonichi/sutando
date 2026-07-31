#!/usr/bin/env python3
"""Tests for morning-briefing.py Google-calendar source preference.

2026-07-21 bug: the briefing announced "your calendar is clear today" off an
empty local macOS Calendar.app read, while the owner had three Google Workspace
meetings that day. The local Calendar.app never had the ag2 work account
subscribed, so its empty read was a *false* empty — the owner rightly called it
bluffing.

Fix: prefer a Google-calendar cache the core agent writes at
`state/calendar-today.json` (the agent can reach the Station connector; this
standalone script cannot). And when MORNING_BRIEFING_CALENDAR_SOURCE=google is
set, the cache is the ONLY trusted source — a missing/stale cache returns None
(→ "couldn't read your calendar"), never a misleading empty local read.

No real osascript / network runs here.
"""
import importlib.util
import json
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "morning-briefing.py"


def _load():
    spec = importlib.util.spec_from_file_location("morning_briefing", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _today():
    return datetime.now().strftime("%Y-%m-%d")


class TestCacheRead(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _point_cache(self, tmp, payload):
        p = Path(tmp) / "calendar-today.json"
        if payload is not None:
            p.write_text(json.dumps(payload))
        self.mod.CALENDAR_CACHE_FILE = p
        return p

    def test_fresh_cache_used_and_no_applescript(self):
        """A today-stamped cache is returned verbatim; osascript is never called."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._point_cache(tmp, {
                "date": _today(),
                "events": [
                    {"raw": "9:30am coworking sutando triage", "calendar": "Work"},
                    {"raw": "10:30am Rui / Qingyun"},
                ],
            })

            def boom(*a, **k):  # osascript must not run
                raise AssertionError("AppleScript path hit despite fresh cache")

            with patch.object(self.mod.subprocess, "run", side_effect=boom):
                events = self.mod.get_calendar_events()

        self.assertEqual(events, [
            {"raw": "9:30am coworking sutando triage", "calendar": "Work"},
            {"raw": "10:30am Rui / Qingyun", "calendar": ""},
        ])

    def test_stale_cache_ignored(self):
        """A cache dated yesterday must not be shown (falls through to None here)."""
        import tempfile
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmp:
            self._point_cache(tmp, {"date": yesterday, "events": [{"raw": "8am Old"}]})
            with patch.dict(os.environ, {"MORNING_BRIEFING_CALENDAR_SOURCE": "google"}):
                events = self.mod.get_calendar_events()
        self.assertIsNone(events)

    def test_corrupt_cache_returns_none_under_google(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "calendar-today.json"
            p.write_text("{not json")
            self.mod.CALENDAR_CACHE_FILE = p
            with patch.dict(os.environ, {"MORNING_BRIEFING_CALENDAR_SOURCE": "google"}):
                events = self.mod.get_calendar_events()
        self.assertIsNone(events)

    def test_empty_cache_is_verified_empty(self):
        """A today cache with an empty events list is a TRUSTED empty ([]), not None."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._point_cache(tmp, {"date": _today(), "events": []})

            def boom(*a, **k):
                raise AssertionError("AppleScript path hit despite present cache")

            with patch.object(self.mod.subprocess, "run", side_effect=boom):
                events = self.mod.get_calendar_events()
        self.assertEqual(events, [])

    def test_non_dict_payload_ignored(self):
        """A cache whose top level is a JSON list (not an object) → None."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._point_cache(tmp, ["not", "an", "object"])
            self.assertIsNone(self.mod._read_calendar_cache())

    def test_events_not_a_list_ignored(self):
        """A today cache whose `events` is not a list → None (malformed)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._point_cache(tmp, {"date": _today(), "events": "9am Thing"})
            self.assertIsNone(self.mod._read_calendar_cache())

    def test_missing_file_returns_none(self):
        """No cache file at all → None (the common default path)."""
        self.mod.CALENDAR_CACHE_FILE = Path("/nonexistent/dir/calendar-today.json")
        self.assertIsNone(self.mod._read_calendar_cache())

    def test_string_events_and_blank_raw(self):
        """Non-dict (bare string) events are accepted; blank/empty raw is dropped."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._point_cache(tmp, {
                "date": _today(),
                "events": [
                    "9:00am Standup",              # bare string → coerced
                    {"raw": "  ", "calendar": "X"},  # blank raw → dropped
                    {"raw": "10:00am Review"},       # dict, no calendar
                    "   ",                            # blank string → dropped
                ],
            })
            events = self.mod._read_calendar_cache()
        self.assertEqual(events, [
            {"raw": "9:00am Standup", "calendar": ""},
            {"raw": "10:00am Review", "calendar": ""},
        ])


class TestGoogleSourceGate(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        # No cache file present.
        self.mod.CALENDAR_CACHE_FILE = Path("/nonexistent/calendar-today.json")

    def test_google_source_no_cache_returns_none_not_local(self):
        """MORNING_BRIEFING_CALENDAR_SOURCE=google + no cache → None; never a
        misleading empty local read. osascript must not be consulted."""
        def boom(*a, **k):
            raise AssertionError("Fell back to AppleScript under google source")

        with patch.dict(os.environ, {"MORNING_BRIEFING_CALENDAR_SOURCE": "google"}), \
             patch.object(self.mod.subprocess, "run", side_effect=boom):
            events = self.mod.get_calendar_events()
        self.assertIsNone(events)

    def test_no_env_falls_back_to_local(self):
        """Without the env, behavior is unchanged: the AppleScript path runs."""
        import subprocess
        ok = subprocess.CompletedProcess(
            args=["osascript"], returncode=0, stdout="Work\t9:00am Planning\n", stderr=""
        )
        # Ensure the env var is absent for this case.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MORNING_BRIEFING_CALENDAR_SOURCE", None)
            with patch.object(self.mod.subprocess, "run", return_value=ok):
                events = self.mod.get_calendar_events()
        self.assertEqual(events, [{"raw": "9:00am Planning", "calendar": "Work"}])

    def test_synthesize_none_under_google_says_couldnt_read(self):
        """End-to-end honesty: no trusted source → 'couldn't read', never 'clear'."""
        text = self.mod.synthesize(
            weather=None, events=None, reminders=[], discord_msgs=[],
            pending_qs=[], health_issues=[],
        )
        self.assertIn("couldn't read your calendar", text)
        self.assertNotIn("clear", text)


if __name__ == "__main__":
    _r = unittest.main(exit=False)
    # Flush coverage data BEFORE os._exit — os._exit skips the atexit handler
    # coverage.py uses to write its .coverage fragment, so without this the
    # coverage gate sees zero data for this file and reds diff-cover.
    try:
        import coverage
        _cov = coverage.Coverage.current()
        if _cov is not None:
            _cov.save()
    except Exception:
        pass
    os._exit(0 if _r.result.wasSuccessful() else 1)
