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
        self.assertFalse(self.mod._all_starts_known(events))

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


class TestPartialAndUnsortedStarts(unittest.TestCase):
    """Both repros from the CHANGES_REQUESTED review on this PR.

    A time claim describes the whole day, so partial knowledge cannot support
    one: with a known-past event plus an unknown-time event the briefing said
    "all earlier — last was unknown-time meeting", and on unsorted all-past
    input it named events[-1] ("last was oldest past") rather than the latest.
    Both are the confident-but-unverified calendar claim this PR removes.
    """

    def setUp(self):
        self.mod = _load(SCRIPT, "morning_briefing")
        self.now = datetime.now().astimezone()

    def _say(self, events):
        return self.mod.synthesize("W", events, [], [], [], [])

    def _ev(self, mins, label, start=True):
        ev = {"raw": label, "calendar": "work"}
        if start:
            ev["start"] = _iso(self.now + timedelta(minutes=mins))
        return ev

    def test_partial_starts_never_claim_all_earlier(self):
        out = self._say([self._ev(-300, "8am known past"),
                         self._ev(0, "unknown-time meeting", start=False)])
        self.assertNotIn("all earlier", out)
        self.assertIn("First up: 8am known past", out)

    def test_partial_starts_never_claim_next_up_either(self):
        """Same reasoning: an unknown-time event may precede the known one."""
        out = self._say([self._ev(0, "unknown-time", start=False),
                         self._ev(45, "8am Zoom")])
        self.assertNotIn("Next up", out)
        self.assertNotIn("all earlier", out)

    def test_last_is_selected_by_time_not_list_order(self):
        out = self._say([self._ev(-30, "newest past"), self._ev(-300, "oldest past")])
        self.assertIn("all earlier", out)
        self.assertIn("last was newest past", out)
        self.assertNotIn("oldest past", out)

    def test_next_is_selected_by_time_on_unsorted_input(self):
        out = self._say([self._ev(300, "later today"), self._ev(20, "soon"),
                         self._ev(-60, "past")])
        self.assertIn("Next up: soon", out)

    def test_all_starts_known_requires_every_event(self):
        known = [self._ev(-10, "a"), self._ev(-20, "b")]
        self.assertTrue(self.mod._all_starts_known(known))
        self.assertFalse(self.mod._all_starts_known(known + [self._ev(0, "c", start=False)]))
        self.assertFalse(self.mod._all_starts_known([]))

    def test_last_event_ignores_undated_entries(self):
        evs = [self._ev(-30, "newest"), self._ev(0, "undated", start=False)]
        self.assertEqual(self.mod._last_event(evs)["raw"], "newest")
        self.assertIsNone(self.mod._last_event([self._ev(0, "x", start=False)]))


class TestSpokenSentence(unittest.TestCase):
    """Assert the sentence the owner actually hears.

    The helper tests above prove the selection; these prove the wording that
    reaches him, which is the thing that was wrong. `synthesize()` is a plain
    function, so this needs no I/O.
    """

    def setUp(self):
        self.mod = _load(SCRIPT, "morning_briefing")
        self.now = datetime.now().astimezone()

    def _say(self, events):
        return self.mod.synthesize("60°F and clear, high of 70, low of 55",
                                   events, [], [], [], [])

    def _ev(self, offset_min, label, start=True):
        ev = {"raw": label, "calendar": "work"}
        if start:
            ev["start"] = _iso(self.now + timedelta(minutes=offset_min))
        return ev

    def test_says_next_up_and_names_the_upcoming_meeting(self):
        out = self._say([self._ev(-420, "12:30am AG2 Connect"),
                         self._ev(45, "8am Yiran Wu's Zoom")])
        self.assertIn("Next up: 8am Yiran Wu's Zoom", out)
        self.assertNotIn("First up", out)
        self.assertNotIn("AG2 Connect", out)

    def test_all_past_says_so_instead_of_naming_a_finished_meeting(self):
        out = self._say([self._ev(-300, "8am standup"), self._ev(-60, "11am sync")])
        self.assertIn("all earlier", out)
        self.assertNotIn("Next up", out)
        self.assertNotIn("First up", out)

    def test_without_start_times_the_wording_is_unchanged(self):
        """The macOS fallback and piped connector events must read exactly as
        they did before this change."""
        out = self._say([self._ev(0, "9am standup", start=False),
                         self._ev(0, "2pm review", start=False)])
        self.assertIn("First up: 9am standup", out)
        self.assertNotIn("Next up", out)

    def test_single_meeting_wording_untouched(self):
        out = self._say([self._ev(60, "8am Zoom")])
        self.assertIn("One meeting today: 8am Zoom", out)

    def test_empty_calendar_still_clear(self):
        self.assertIn("clear today", self._say([]))

    def test_unreadable_calendar_still_reported(self):
        self.assertIn("couldn't read your calendar", self._say(None))


class TestAllDayDateOnlyStarts(unittest.TestCase):
    """A bare YYYY-MM-DD start is an ALL-DAY event: the day is known, the clock
    time is not, so midnight reads as already-past for the rest of the day.
    """

    def setUp(self):
        self.mod = _load(SCRIPT, "morning_briefing")
        self.today = datetime.now().astimezone().date().isoformat()

    def _say(self, events):
        return self.mod.synthesize("W", events, [], [], [], [])

    def test_a_date_only_start_is_unknown_not_midnight(self):
        self.assertIsNone(self.mod._parse_start({"start": self.today}))

    def test_all_day_events_do_not_count_as_known_starts(self):
        evs = [{"raw": "all-day offsite", "start": self.today},
               {"raw": "all-day training", "start": self.today}]
        self.assertFalse(self.mod._all_starts_known(evs))

    def test_two_all_day_events_never_claim_all_earlier(self):
        # The exact-head repro: both starts equal today's local date produced
        # "2 meetings today, all earlier — last was all-day offsite."
        out = self._say([{"raw": "all-day offsite", "calendar": "work",
                          "start": self.today},
                         {"raw": "all-day training", "calendar": "work",
                          "start": self.today}])
        self.assertNotIn("all earlier", out)
        self.assertNotIn("Next up", out)

    def test_an_all_day_event_beside_a_timed_one_stays_conservative(self):
        now = datetime.now().astimezone()
        out = self._say([{"raw": "all-day offsite", "calendar": "work",
                          "start": self.today},
                         {"raw": "8am Zoom", "calendar": "work",
                          "start": _iso(now - timedelta(minutes=300))}])
        self.assertNotIn("all earlier", out)

    def test_time_bearing_starts_are_not_over_rejected(self):
        # The guard keys on the absence of a clock time, so every timed form
        # must still parse — including the space separator and a UTC offset.
        for raw in (f"{self.today}T10:00", f"{self.today}T10:00:00+00:00",
                    f"{self.today} 14:30"):
            self.assertIsNotNone(self.mod._parse_start({"start": raw}), raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
