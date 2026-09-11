#!/usr/bin/env python3
"""Tests for morning-briefing.py calendar failure handling.

Before this fix, an osascript failure (e.g. Calendar.app not running →
"execution error: Calendar got an error: Application isn't running. (-600)")
was swallowed and rendered as 0 events — the briefing said "Your calendar is
clear today", indistinguishable from a verified-empty calendar.

After the fix:
- a failed query launches Calendar.app in the background (`open -gja
  Calendar`) and retries once;
- if the retry also fails, get_calendar_events() returns None and the
  briefing says the calendar couldn't be read — never "clear";
- the success path is unchanged.

All subprocess calls are mocked — no real osascript runs here.
"""
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "morning-briefing.py"

CAL_600_ERR = (
    "execution error: Calendar got an error: "
    "Application isn't running. (-600)"
)


def _load():
    spec = importlib.util.spec_from_file_location("morning_briefing", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _osascript_ok(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["osascript"], returncode=0, stdout=stdout, stderr=""
    )


def _osascript_fail() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["osascript"], returncode=1, stdout="", stderr=CAL_600_ERR
    )


def _open_ok() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["open"], returncode=0, stdout="", stderr="")


class TestCalendarRetry(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        # Force the AppleScript path deterministically: point the Google-calendar
        # cache at a nonexistent file so get_calendar_events() never short-circuits
        # on a real workspace state/calendar-today.json left by the agent.
        self.mod.CALENDAR_CACHE_FILE = Path("/nonexistent/calendar-today.json")

    def test_error_then_retry_succeeds(self):
        """First osascript fails (-600) → Calendar launched → retry returns events."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "open":
                return _open_ok()
            if len([c for c in calls if c[0] == "osascript"]) == 1:
                return _osascript_fail()
            return _osascript_ok("Work\t10:30am Standup\n")

        with patch.object(self.mod.subprocess, "run", side_effect=fake_run), \
             patch.object(self.mod.time, "sleep"):
            events = self.mod.get_calendar_events()

        self.assertEqual(calls[0][0], "osascript")
        self.assertIn(["open", "-gja", "Calendar"], calls)
        self.assertEqual(len([c for c in calls if c[0] == "osascript"]), 2)
        self.assertEqual(events, [{"raw": "10:30am Standup", "calendar": "Work"}])

    def test_launch_failure_still_retries(self):
        """`open -gja Calendar` itself failing must not abort the retry."""
        osascript_calls = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "open":
                raise OSError("open not found")
            osascript_calls.append(cmd)
            if len(osascript_calls) == 1:
                return _osascript_fail()
            return _osascript_ok("Work\t10:30am Standup\n")

        with patch.object(self.mod.subprocess, "run", side_effect=fake_run), \
             patch.object(self.mod.time, "sleep"):
            events = self.mod.get_calendar_events()

        self.assertEqual(len(osascript_calls), 2)
        self.assertEqual(events, [{"raw": "10:30am Standup", "calendar": "Work"}])

    def test_both_attempts_fail_returns_none(self):
        """Both osascript attempts fail → None, not an empty list."""
        def fake_run(cmd, **kwargs):
            if cmd[0] == "open":
                return _open_ok()
            return _osascript_fail()

        with patch.object(self.mod.subprocess, "run", side_effect=fake_run), \
             patch.object(self.mod.time, "sleep"):
            events = self.mod.get_calendar_events()

        self.assertIsNone(events)

    def test_success_path_unchanged(self):
        """First attempt succeeds → events parsed, no launch, no retry."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _osascript_ok("Work\t9:00am Planning\nHome\t6:00pm Dinner\n")

        with patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            events = self.mod.get_calendar_events()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "osascript")
        self.assertEqual(events, [
            {"raw": "9:00am Planning", "calendar": "Work"},
            {"raw": "6:00pm Dinner", "calendar": "Home"},
        ])

    def test_verified_empty_still_returns_empty_list(self):
        """A successful query with no events is [] (verified empty), not None."""
        with patch.object(
            self.mod.subprocess, "run", return_value=_osascript_ok("")
        ):
            events = self.mod.get_calendar_events()
        self.assertEqual(events, [])


class TestSynthesizeCalendarLine(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _synth(self, events):
        return self.mod.synthesize(
            weather=None, events=events, reminders=[], discord_msgs=[],
            pending_qs=[], health_issues=[],
        )

    def test_unavailable_says_couldnt_read(self):
        text = self._synth(None)
        self.assertIn("couldn't read your calendar", text)
        self.assertNotIn("clear", text)
        self.assertNotIn("0 events", text)
        # Unknown calendar state must not be claimed as a clean day.
        self.assertNotIn("Everything looks clean", text)

    def test_verified_empty_still_says_clear(self):
        text = self._synth([])
        self.assertIn("Your calendar is clear today.", text)
        self.assertIn("Everything looks clean", text)

    def test_events_render_unchanged(self):
        text = self._synth([{"raw": "10:30am Standup", "calendar": "Work"}])
        self.assertIn("One meeting today: 10:30am Standup.", text)


class TestMainCalendarStatusLine(unittest.TestCase):
    def test_main_prints_unavailable_not_zero_events(self):
        """main() logs 'calendar: unavailable' (not '0 events') when read fails."""
        import contextlib
        import io
        import tempfile

        mod = _load()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with patch.object(mod, "RESULTS_DIR", tmp / "results"), \
                 patch.object(mod, "STATE_DIR", tmp / "state"), \
                 patch.object(mod, "get_weather", return_value=None), \
                 patch.object(mod, "get_calendar_events", return_value=None), \
                 patch.object(mod, "get_reminders", return_value=[]), \
                 patch.object(mod, "get_overnight_discord", return_value=[]), \
                 patch.object(mod, "get_pending_questions", return_value=[]), \
                 patch.object(mod, "get_health_issues", return_value=[]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    mod.main()
            printed = out.getvalue()
            self.assertIn("calendar: unavailable", printed)
            self.assertNotIn("0 events", printed)
            self.assertIn("couldn't read your calendar", printed)


class TestWeatherLatLonOverride(unittest.TestCase):
    """get_weather() honors WEATHER_LAT/WEATHER_LON via config_get (env legacy
    fallback), exercising the config_get override branch."""

    def setUp(self):
        self.mod = _load()

    class _FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._payload

    _WX = (
        b'{"current":{"temperature_2m":62.4,"weather_code":1},'
        b'"daily":{"temperature_2m_max":[70],"temperature_2m_min":[52],'
        b'"precipitation_probability_max":[10]}}'
    )

    def test_env_latlon_override_used(self):
        import os
        captured = {}

        def fake_urlopen(url, timeout=8):
            captured["url"] = url
            return self._FakeResp(self._WX)

        with patch.dict(os.environ, {"WEATHER_LAT": "47.67", "WEATHER_LON": "-122.12"}), \
             patch.object(self.mod, "_run_applescript", return_value=("America/Los_Angeles", "")), \
             patch.object(self.mod, "urlopen", side_effect=fake_urlopen):
            out = self.mod.get_weather()

        # config_get picked up the override → URL carries the Redmond coords,
        # not the SF default.
        self.assertIn("latitude=47.67", captured["url"])
        self.assertIn("longitude=-122.12", captured["url"])
        self.assertIn("62°F", out)
        self.assertIn("mostly clear", out)

    def test_no_override_uses_default(self):
        import os
        captured = {}

        def fake_urlopen(url, timeout=8):
            captured["url"] = url
            return self._FakeResp(self._WX)

        # Neither env nor config set → default SF coords; config_get returns None
        # so the override branch is skipped.
        env_clear = {k: v for k, v in os.environ.items()
                     if k not in ("WEATHER_LAT", "WEATHER_LON")}
        with patch.dict(os.environ, env_clear, clear=True), \
             patch.object(self.mod, "_run_applescript", return_value=("UTC", "")), \
             patch.object(self.mod, "urlopen", side_effect=fake_urlopen):
            out = self.mod.get_weather()

        self.assertIn("latitude=37.77", captured["url"])
        self.assertIn("62°F", out)


if __name__ == "__main__":
    # Hard-exit after the suite to sidestep a Python interpreter-teardown SIGSEGV
    # on ubuntu-latest runners: the 9 tests pass, then the process segfaults during
    # interpreter shutdown (not the test logic - subprocess calls are mocked).
    import os
    _r = unittest.main(exit=False)
    # os._exit() skips atexit, which is where coverage.py writes its data file —
    # so without an explicit save the lines this suite exercises (incl. main()'s
    # briefing write) record as UNCOVERED under the coverage gate even though the
    # tests ran and passed (#1832 class). Flush the active session first.
    try:
        import coverage
        _c = coverage.Coverage.current()
        if _c is not None:
            _c.save()
    except Exception:
        pass
    os._exit(0 if _r.result.wasSuccessful() else 1)
