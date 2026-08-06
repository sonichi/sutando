#!/usr/bin/env python3
"""The briefing must announce your NEXT meeting, not the day's first.

Observed on Chis-Mac-mini 2026-08-06 at 07:32 local, from a real run:

    "11 meetings today. First up: 12:30am-1:30am AG2 Connect (PST) @ Discord ..."

That meeting had ended ~7 hours earlier. The line came from `events[0]` — the
first event of the *day* — assigned to a variable already named `next_ev`, so
the intent was right and only the selection was wrong. The owner's actual next
meeting was 8:00am.

The start time needed to fix it was already being computed: `events_from_gws()`
built a `_sort` key from the event's ISO start, sorted on it, and then popped it
off before writing the cache. This suite pins the whole path, because the field
has to survive three hops — producer, cache file, reader — and dropping it at
any one of them leaves the feature inert while every part looks correct.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "morning-briefing.py"
WRITER = REPO / "src" / "write_calendar_cache.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(dt):
    return dt.astimezone().isoformat()


class TestNextEventSelection(unittest.TestCase):
    def setUp(self):
        self.mod = _load(SCRIPT, "morning_briefing")
        self.now = datetime.now().astimezone()

    def _ev(self, offset_min, label):
        return {"raw": f"{label}", "calendar": "work",
                "start": _iso(self.now + timedelta(minutes=offset_min))}

    def test_picks_the_next_event_not_the_days_first(self):
        """The load-bearing case: a long-finished first meeting must not win."""
        events = [self._ev(-420, "12:30am AG2 Connect"),
                  self._ev(-2, "7:30am write"),
                  self._ev(30, "8:00am Yiran Wu's Zoom"),
                  self._ev(500, "MassGen daily")]
        nxt = self.mod._next_event(events)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["raw"], "8:00am Yiran Wu's Zoom")

    def test_earliest_future_wins_even_if_list_is_unsorted(self):
        events = [self._ev(300, "late"), self._ev(20, "soon"), self._ev(-60, "past")]
        self.assertEqual(self.mod._next_event(events)["raw"], "soon")

    def test_all_past_returns_none_rather_than_the_first(self):
        events = [self._ev(-300, "a"), self._ev(-30, "b")]
        self.assertIsNone(self.mod._next_event(events))

    def test_events_without_start_are_not_assumed_upcoming(self):
        """Piped connector events and the macOS fallback carry no start. They
        must not be silently treated as future — that would announce an
        arbitrary meeting as next."""
        events = [{"raw": "no start", "calendar": ""}]
        self.assertIsNone(self.mod._next_event(events))
        self.assertFalse(self.mod._any_start(events))

    def test_unparseable_start_is_ignored_not_epoch(self):
        events = [{"raw": "junk", "calendar": "", "start": "not-a-date"},
                  self._ev(15, "real")]
        self.assertEqual(self.mod._next_event(events)["raw"], "real")
        self.assertIsNone(self.mod._parse_start(events[0]))


class TestStartSurvivesTheCache(unittest.TestCase):
    """The latent-no-op guard.

    `_read_calendar_cache()` REBUILDS each event dict instead of passing it
    through, so a field the producer writes is dropped unless it is named there
    too. Without this test the fix would ship complete-looking and inert.
    """

    def setUp(self):
        self.mod = _load(SCRIPT, "morning_briefing")

    def test_reader_preserves_start_from_the_cache_file(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "calendar-today.json"
            start = _iso(datetime.now().astimezone() + timedelta(hours=1))
            cache.write_text(json.dumps({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "events": [{"raw": "8am Zoom", "calendar": "work", "start": start}],
            }))
            with patch.object(self.mod, "CALENDAR_CACHE_FILE", cache):
                events = self.mod._read_calendar_cache()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get("start"), start,
                         "reader dropped `start`; _next_event can never fire")

    def test_reader_still_accepts_events_without_start(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "calendar-today.json"
            cache.write_text(json.dumps({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "events": [{"raw": "9:30am Standup"}],
            }))
            with patch.object(self.mod, "CALENDAR_CACHE_FILE", cache):
                events = self.mod._read_calendar_cache()
        self.assertEqual(events, [{"raw": "9:30am Standup", "calendar": ""}])


class TestWriterKeepsStart(unittest.TestCase):
    def setUp(self):
        self.writer = _load(WRITER, "write_calendar_cache")

    def test_normalize_preserves_start_when_supplied(self):
        out = self.writer.normalize_events(
            [{"raw": "8am Zoom", "calendar": "work", "start": "2026-08-06T08:00:00-07:00"}])
        self.assertEqual(out[0]["start"], "2026-08-06T08:00:00-07:00")

    def test_normalize_omits_start_when_absent(self):
        """Piped events have no start; the key must not appear as empty string,
        which `_parse_start` would then have to special-case."""
        out = self.writer.normalize_events([{"raw": "8am Zoom", "calendar": "work"}])
        self.assertNotIn("start", out[0])
        self.assertEqual(out[0], {"raw": "8am Zoom", "calendar": "work"})

    def test_plain_string_events_still_work(self):
        out = self.writer.normalize_events(["9:30am Standup"])
        self.assertEqual(out, [{"raw": "9:30am Standup", "calendar": ""}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
