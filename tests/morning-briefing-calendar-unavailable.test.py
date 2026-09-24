#!/usr/bin/env python3
"""Tests for morning-briefing.py calendar failure handling and the native opt-in.

The local macOS Calendar.app read raises a macOS Automation permission prompt,
so an unattended cron must never reach it unasked (user report, 2026-09-24: the
desktop app raised a Calendar prompt nobody asked for). Contract:

- without MORNING_BRIEFING_CALENDAR_SOURCE=macos (or SUTANDO_ALLOW_NATIVE_PIM=1)
  no osascript / `open` subprocess runs; the briefing names the missing source;
- opted in: ONE AppleScript read, no `open -gja Calendar`, no retry;
- a stored denial (-1743) is final: recorded once in state/, later runs skip the
  read, and the briefing says so instead of "clear";
- get_calendar_events() returns None on failure and the briefing says the
  calendar couldn't be read — never "clear".

All subprocess calls are mocked — no real osascript runs here.
"""
import importlib.util
import os
import subprocess
import tempfile
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


CAL_1743_ERR = (
    "execution error: Not authorized to send Apple events to Calendar. (-1743)"
)

OPT_IN = {"MORNING_BRIEFING_CALENDAR_SOURCE": "macos"}


def _env_without_optin():
    env = {k: v for k, v in os.environ.items()
           if k not in ("MORNING_BRIEFING_CALENDAR_SOURCE", "SUTANDO_ALLOW_NATIVE_PIM")}
    return patch.dict(os.environ, env, clear=True)


class TestNativeCalendarGate(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        # Point the cache at nothing so a real workspace state/calendar-today.json
        # left by the agent can never short-circuit these cases.
        self.mod.CALENDAR_CACHE_FILE = Path("/nonexistent/calendar-today.json")
        self._tmp = tempfile.TemporaryDirectory()
        self.mod.STATE_DIR = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_not_opted_in_never_spawns_and_names_the_missing_source(self):
        """Default host: no osascript, no `open`; None with the no-source note."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            raise AssertionError(f"subprocess reached without opt-in: {cmd}")

        with _env_without_optin(), patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            events = self.mod.get_calendar_events()
        self.assertIsNone(events)
        self.assertEqual(calls, [])
        self.assertEqual(self.mod.CALENDAR_UNREAD_NOTE, self.mod.NO_CALENDAR_SOURCE_NOTE)
        text = self.mod.synthesize(weather=None, events=events, reminders=[],
                                   discord_msgs=[], pending_qs=[], health_issues=[])
        self.assertIn("connect Google Calendar via Settings → Apps → Integrations", text)
        self.assertNotIn("clear", text)

    def test_google_source_wins_over_native_escape_hatch(self):
        """google pins the cache: SUTANDO_ALLOW_NATIVE_PIM=1 must not open a local read."""
        def boom(cmd, **kwargs):
            raise AssertionError(f"local read under google source: {cmd}")

        with patch.dict(os.environ, {"MORNING_BRIEFING_CALENDAR_SOURCE": "google",
                                     "SUTANDO_ALLOW_NATIVE_PIM": "1"}), \
             patch.object(self.mod.subprocess, "run", side_effect=boom):
            self.assertIsNone(self.mod.get_calendar_events())
        self.assertIsNone(self.mod.CALENDAR_UNREAD_NOTE)

    def test_opted_in_reads_once_without_launching(self):
        """macos opt-in: exactly one osascript call, never `open -gja Calendar`."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _osascript_ok("Work\t9:00am Planning\nHome\t6:00pm Dinner\n")

        with patch.dict(os.environ, OPT_IN), \
             patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            events = self.mod.get_calendar_events()

        self.assertEqual([c[0] for c in calls], ["osascript"])
        self.assertEqual(events, [
            {"raw": "9:00am Planning", "calendar": "Work"},
            {"raw": "6:00pm Dinner", "calendar": "Home"},
        ])

    def test_escape_hatch_env_also_opts_in(self):
        with patch.dict(os.environ, {"SUTANDO_ALLOW_NATIVE_PIM": "1"}), \
             patch.object(self.mod.subprocess, "run", return_value=_osascript_ok("Work\t10:30am Standup\n")):
            os.environ.pop("MORNING_BRIEFING_CALENDAR_SOURCE", None)
            events = self.mod.get_calendar_events()
        self.assertEqual(events, [{"raw": "10:30am Standup", "calendar": "Work"}])

    def test_failed_read_is_none_with_no_retry_and_no_launch(self):
        """A -600 failure no longer launches Calendar.app or retries: one call, None."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _osascript_fail()

        with patch.dict(os.environ, OPT_IN), \
             patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            events = self.mod.get_calendar_events()

        self.assertIsNone(events)
        self.assertEqual([c[0] for c in calls], ["osascript"])
        self.assertFalse(self.mod._calendar_denied_marker().exists())
        self.assertIsNone(self.mod.CALENDAR_UNREAD_NOTE)

    def test_denial_is_recorded_once_and_never_retried(self):
        """-1743: one read, the marker is written, the next run skips osascript entirely."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr=CAL_1743_ERR)

        with patch.dict(os.environ, OPT_IN), \
             patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            first = self.mod.get_calendar_events()
            second = self.mod.get_calendar_events()

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(calls), 1, "a stored denial must not be re-asked")
        self.assertTrue(self.mod._calendar_denied_marker().exists())
        self.assertEqual(self.mod.CALENDAR_UNREAD_NOTE, self.mod.CALENDAR_DENIED_NOTE)
        text = self.mod.synthesize(weather=None, events=second, reminders=[],
                                   discord_msgs=[], pending_qs=[], health_issues=[])
        self.assertIn("denied Calendar access", text)
        self.assertIn("won't ask again", text)
        self.assertNotIn("clear", text)

    def test_denial_marker_unwritable_still_returns_none(self):
        """A read-only state dir must not turn the denial into a crash."""
        self.mod.STATE_DIR = Path("/nonexistent/ro-state")
        with patch.dict(os.environ, OPT_IN), \
             patch.object(self.mod.subprocess, "run",
                          return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=CAL_1743_ERR)):
            self.assertIsNone(self.mod.get_calendar_events())
        self.assertEqual(self.mod.CALENDAR_UNREAD_NOTE, self.mod.CALENDAR_DENIED_NOTE)

    def test_verified_empty_still_returns_empty_list(self):
        """A successful query with no events is [] (verified empty), not None."""
        with patch.dict(os.environ, OPT_IN), \
             patch.object(self.mod.subprocess, "run", return_value=_osascript_ok("")):
            events = self.mod.get_calendar_events()
        self.assertEqual(events, [])


class TestRemindersGate(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_not_opted_in_never_spawns_reminders(self):
        def boom(cmd, **kwargs):
            raise AssertionError(f"reminders.py reached without opt-in: {cmd}")

        with _env_without_optin(), patch.object(self.mod.subprocess, "run", side_effect=boom):
            self.assertIsNone(self.mod.get_reminders())

    def test_opted_in_passes_owner_asked(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0,
                                               stdout="  [Work] today task (due x)\n", stderr="")

        with patch.dict(os.environ, OPT_IN), \
             patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            items = self.mod.get_reminders()
        self.assertIn("--owner-asked", seen["cmd"])
        self.assertEqual(items, ["[Work] today task (due x)"])


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
    # on ubuntu-latest runners: the tests pass, then the process segfaults during
    # interpreter shutdown (not the test logic - subprocess calls are mocked).
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
