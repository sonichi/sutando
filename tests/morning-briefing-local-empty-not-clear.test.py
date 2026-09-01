#!/usr/bin/env python3
"""An empty local Calendar.app read is not a verified-empty day.

2026-08-28: the briefing opened with "Your calendar is clear today." The owner
had five meetings, one already in progress. The Google cache was four days
stale, so `get_calendar_events()` fell back to local Calendar.app — which on
that host holds a "Work" calendar carrying none of the Google work account.
Measured there: 0 events across all 7 local calendars for the day and 0 in the
Work calendar over a 60-day window, against 8 real Google events that morning.

`[]` from that source means "I cannot see it", not "the day is clear". A cache
file that exists but is not today's is the evidence that distinguishes the two:
it says the owner's real calendar lives in Google, so the local read is blind.

The macOS-only host is unaffected — with no cache file ever written, an empty
local read stays a verified empty.

No real osascript / network runs here.
"""
import importlib.util
import json
import tempfile
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


class _R:
    def __init__(self, out=""):
        self.returncode, self.stdout, self.stderr = 0, out, ""


class TestLocalEmptyIsNotClear(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "calendar-today.json"
        self.mod.CALENDAR_CACHE_FILE = self.cache

    def _stale_cache(self):
        y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.cache.write_text(json.dumps({"date": y, "events": [{"raw": "8am Old"}]}))

    def _run(self, applescript_out=""):
        """Every case drives the SAME successful, empty osascript result, so the
        only variable across cases is what is on disk."""
        with patch.object(self.mod.subprocess, "run",
                          return_value=_R(applescript_out)):
            return self.mod.get_calendar_events()

    def test_stale_cache_plus_empty_local_read_is_unreadable(self):
        """The reported shape: must be None, never [] (which renders 'clear')."""
        self._stale_cache()
        self.assertIsNone(self._run())

    def test_no_cache_ever_keeps_verified_empty(self):
        """Same stub, no cache file: a macOS-only host still reports [] ."""
        self.assertFalse(self.cache.exists())
        self.assertEqual(self._run(), [])

    def test_stale_cache_does_not_suppress_real_local_events(self):
        """The guard fires only on an EMPTY read — present events pass through."""
        self._stale_cache()
        self.assertEqual(
            self._run("Work\t9:00am Planning\n"),
            [{"raw": "9:00am Planning", "calendar": "Work"}],
        )

    def test_corrupt_cache_is_blind_not_clear(self):
        """A file that exists but will not parse is still evidence this host
        writes one. Unreadable is a reason to distrust an empty local read."""
        self.cache.write_text("{not json")
        self.assertIsNone(self._run())

    def test_cache_without_a_date_key_is_blind_not_clear(self):
        """Schema drift that drops `date` must not read as never-configured."""
        self.cache.write_text(json.dumps({"events": []}))
        self.assertIsNone(self._run())

    def test_an_empty_file_is_blind_not_clear(self):
        """A crash mid-write leaves zero bytes: parses as nothing, exists."""
        self.cache.write_text("")
        self.assertIsNone(self._run())

    def test_a_dangling_symlink_is_blind_not_clear(self):
        """A broken symlink IS a cache this host writes, but `exists()` follows
        it and answers False — the false-clear reached by filesystem shape."""
        self.cache.symlink_to(Path(self.tmp.name) / "never-written.json")
        self.assertFalse(self.cache.exists())     # the trap the old check fell into
        self.assertTrue(self.cache.is_symlink())  # yet the path is plainly present
        self.assertIsNone(self._run())

    def test_a_truly_absent_cache_still_reports_verified_empty(self):
        """Negative control for the case above: without it, a fix that returned
        True unconditionally would pass and leave every host permanently blind."""
        self.assertFalse(self.cache.is_symlink())
        self.assertFalse(self.cache.exists())
        self.assertEqual(self._run(), [])

    def test_rendering_differs_between_the_two_states(self):
        """Pin the user-visible property: one says clear, the other does not."""
        def line(events):
            return self.mod.synthesize(
                weather=None, events=events, reminders=[], discord_msgs=[],
                pending_qs=[], health_issues=[],
            )

        self._stale_cache()
        blind = line(self._run())
        self.cache.unlink()
        empty = line(self._run())

        self.assertIn("clear", empty.lower())
        self.assertNotIn("clear", blind.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
