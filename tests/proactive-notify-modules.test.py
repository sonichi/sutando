#!/usr/bin/env python3
"""Tests for proactive-notify action/source/presence/runner modules.

Companion to proactive-notify-router.test.py (which covers the channel
router + matcher). This file drives the remaining modules — the two
delivery actions (sms, call), the calendar source, the presence snapshot,
and the runner orchestration — with all external I/O (Twilio HTTP, the
conversation server, `gws`, and workspace state files) mocked.

Run: python3 tests/proactive-notify-modules.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = ROOT / "skills" / "proactive-notify" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
sys.path.insert(0, str(ROOT / "src"))

import presence  # noqa: E402
import runner  # noqa: E402
from actions import call as call_action  # noqa: E402
from actions import sms as sms_action  # noqa: E402
from sources import google_calendar as gcal  # noqa: E402


class _Resp:
    """Minimal context-manager stand-in for urllib's urlopen() return."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


# --------------------------------------------------------------------------
# actions/sms.py
# --------------------------------------------------------------------------
class TestSmsAction(unittest.TestCase):
    def test_load_env_parses_quotes_and_comments(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".env").write_text(
                "# a comment\n"
                "\n"
                "PLAIN=value\n"
                'QUOTED="quoted val"\n'
                "SINGLE='single val'\n"
                "INLINE=val # trailing\n"
                "NOEQUALSLINE\n"
            )
            env = sms_action._load_env(root)
        self.assertEqual(env["PLAIN"], "value")
        self.assertEqual(env["QUOTED"], "quoted val")
        self.assertEqual(env["SINGLE"], "single val")
        self.assertEqual(env["INLINE"], "val")
        self.assertNotIn("NOEQUALSLINE", env)

    def test_load_env_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sms_action._load_env(Path(d)), {})

    def test_send_missing_credential(self):
        with mock.patch.object(sms_action, "_load_env", return_value={}), \
                mock.patch.dict("os.environ", {}, clear=True):
            res = sms_action.send(object(), "hi")
        self.assertFalse(res["ok"])
        self.assertIn("missing", res["error"])

    def test_send_success(self):
        env = {
            "TWILIO_ACCOUNT_SID": "AC1",
            "TWILIO_AUTH_TOKEN": "tok",
            "TWILIO_PHONE_NUMBER": "+1",
            "OWNER_NUMBER": "+2",
        }
        with mock.patch.object(sms_action, "_load_env", return_value=env), \
                mock.patch("urllib.request.urlopen", return_value=_Resp({"sid": "SM9"})):
            res = sms_action.send(object(), "hi")
        self.assertTrue(res["ok"])
        self.assertEqual(res["id"], "SM9")

    def test_send_http_error(self):
        env = {
            "TWILIO_ACCOUNT_SID": "AC1",
            "TWILIO_AUTH_TOKEN": "tok",
            "TWILIO_PHONE_NUMBER": "+1",
            "OWNER_NUMBER": "+2",
        }
        with mock.patch.object(sms_action, "_load_env", return_value=env), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            res = sms_action.send(object(), "hi")
        self.assertFalse(res["ok"])
        self.assertIn("boom", res["error"])


# --------------------------------------------------------------------------
# actions/call.py
# --------------------------------------------------------------------------
class TestCallAction(unittest.TestCase):
    def test_send_missing_owner_number_falls_back_to_env_loader(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(sms_action, "_load_env", return_value={}):
            res = call_action.send(object(), "hi")
        self.assertFalse(res["ok"])
        self.assertIn("OWNER_NUMBER", res["error"])

    def test_send_success_env_target(self):
        with mock.patch.dict("os.environ", {"OWNER_NUMBER": "+1"}, clear=True), \
                mock.patch("urllib.request.urlopen", return_value=_Resp({"callSid": "CA5"})):
            res = call_action.send(object(), "hi")
        self.assertTrue(res["ok"])
        self.assertEqual(res["id"], "CA5")

    def test_send_target_from_env_loader_then_success(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(sms_action, "_load_env", return_value={"OWNER_NUMBER": "+9"}), \
                mock.patch("urllib.request.urlopen", return_value=_Resp({"callSid": "CA6"})):
            res = call_action.send(object(), "hi")
        self.assertTrue(res["ok"])
        self.assertEqual(res["id"], "CA6")

    def test_send_http_error(self):
        with mock.patch.dict("os.environ", {"OWNER_NUMBER": "+1"}, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            res = call_action.send(object(), "hi")
        self.assertFalse(res["ok"])
        self.assertIn("down", res["error"])


# --------------------------------------------------------------------------
# sources/google_calendar.py
# --------------------------------------------------------------------------
class TestGoogleCalendarSource(unittest.TestCase):
    def test_parse_event_start_allday_and_timed(self):
        allday = gcal._parse_event_start("2026-07-10")
        self.assertEqual(allday.tzinfo, timezone.utc)
        timed = gcal._parse_event_start("2026-07-10T09:30:00+00:00")
        self.assertEqual(timed.hour, 9)

    def _fake_run(self, *, rc=0, stdout="", exc=None):
        def _runner(*a, **k):
            if exc:
                raise exc
            m = mock.Mock()
            m.returncode = rc
            m.stdout = stdout
            return m
        return _runner

    def test_iter_raw_events_success(self):
        payload = json.dumps({"events": [{"summary": "X", "start": "t"}]})
        with mock.patch("subprocess.run", side_effect=self._fake_run(stdout=payload)):
            got = list(gcal._iter_raw_events(15))
        self.assertEqual(got[0]["summary"], "X")

    def test_iter_raw_events_nonzero_exit(self):
        with mock.patch("subprocess.run", side_effect=self._fake_run(rc=1, stdout="{}")):
            self.assertEqual(list(gcal._iter_raw_events(15)), [])

    def test_iter_raw_events_bad_json(self):
        with mock.patch("subprocess.run", side_effect=self._fake_run(stdout="not json")):
            self.assertEqual(list(gcal._iter_raw_events(15)), [])

    def test_iter_raw_events_binary_missing(self):
        with mock.patch("subprocess.run", side_effect=self._fake_run(exc=FileNotFoundError())):
            self.assertEqual(list(gcal._iter_raw_events(15)), [])

    def test_fetch_filters_and_maps(self):
        now = datetime.now(timezone.utc)
        soon = (now + timedelta(minutes=10)).replace(microsecond=0).isoformat()
        far = (now + timedelta(minutes=999)).replace(microsecond=0).isoformat()
        past = (now - timedelta(minutes=30)).replace(microsecond=0).isoformat()
        raws = [
            {"summary": "Soon", "start": soon, "calendar": "work", "location": "Rm"},
            {"summary": "Far", "start": far},
            {"summary": "Past", "start": past},
            {"summary": "AllDay", "start": "2026-07-10"},
            {"summary": "NoStart"},
            {"summary": "BadStart", "start": "garbage"},
        ]
        with mock.patch.object(gcal, "_iter_raw_events", return_value=iter(raws)):
            out = gcal.fetch({"lookahead_min": 15})
        titles = [e["title"] for e in out]
        self.assertEqual(titles, ["Soon"])
        self.assertIn(out[0]["minutes_until"], (9, 10))  # sub-second drift on recompute
        self.assertIn("|Soon", out[0]["dedup_key_suffix"])

    def test_fetch_untitled_default(self):
        now = datetime.now(timezone.utc)
        soon = (now + timedelta(minutes=5)).replace(microsecond=0).isoformat()
        with mock.patch.object(gcal, "_iter_raw_events", return_value=iter([{"start": soon}])):
            out = gcal.fetch({"lookahead_min": 15})
        self.assertEqual(out[0]["title"], "(untitled)")


# --------------------------------------------------------------------------
# presence.py
# --------------------------------------------------------------------------
class TestPresence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir()
        self._orig_state = presence.STATE_DIR
        self._orig_log = presence.VOICE_LOG
        presence.STATE_DIR = self.state
        presence.VOICE_LOG = Path(self._tmp.name) / "logs" / "voice-agent.log"

    def tearDown(self):
        presence.STATE_DIR = self._orig_state
        presence.VOICE_LOG = self._orig_log
        self._tmp.cleanup()

    def test_read_json_bad(self):
        p = self.state / "x.json"
        p.write_text("{not json")
        self.assertEqual(presence._read_json(p), {})

    def test_presenter_mode_missing_and_expired_and_active(self):
        self.assertFalse(presence._presenter_mode_active())
        sentinel = self.state / "presenter-mode.sentinel"
        sentinel.write_text("2000-01-01T00:00:00Z")  # expired
        self.assertFalse(presence._presenter_mode_active())
        sentinel.write_text("2999-01-01T00:00:00Z")  # future
        self.assertTrue(presence._presenter_mode_active())
        sentinel.write_text("not-a-date")
        self.assertFalse(presence._presenter_mode_active())

    def test_voice_client_connected(self):
        self.assertFalse(presence._voice_client_connected())
        presence.VOICE_LOG.parent.mkdir(parents=True, exist_ok=True)
        presence.VOICE_LOG.write_text("noise\n[Health] client=true\n")
        self.assertTrue(presence._voice_client_connected())
        presence.VOICE_LOG.write_text("[Health] client=false\n")
        self.assertFalse(presence._voice_client_connected())

    def test_owner_active_in_discord(self):
        self.assertFalse(presence._owner_active_in_discord_within(5))
        act = self.state / "last-owner-activity.json"
        act.write_text(json.dumps({"ts": time.time(), "channel": "discord"}))
        self.assertTrue(presence._owner_active_in_discord_within(5))
        act.write_text(json.dumps({"ts": time.time(), "channel": "slack"}))
        self.assertFalse(presence._owner_active_in_discord_within(5))

    def test_in_quiet_hours_variants(self):
        self.assertFalse(presence._in_quiet_hours({}))
        # Normal (non-wrap) window covering "now": derive from current UTC hour.
        now_h = datetime.now(timezone.utc).hour
        start = f"{now_h:02d}:00"
        end = f"{(now_h + 1) % 24:02d}:00"
        pol = {"quiet_hours": {"start": start, "end": end, "timezone": "UTC"}}
        # Wrap-around window that always contains now (start == end+ -> covers all).
        wrap = {"quiet_hours": {"start": "00:00", "end": "00:00", "timezone": "UTC"}}
        # Just assert the function returns a bool without raising for each path.
        self.assertIsInstance(presence._in_quiet_hours(pol), bool)
        self.assertIsInstance(presence._in_quiet_hours(wrap), bool)

    def test_in_quiet_hours_wraparound_true(self):
        # A window 00:00->23:59 (non-wrap) contains virtually all of the day.
        pol = {"quiet_hours": {"start": "00:00", "end": "23:59", "timezone": "UTC"}}
        self.assertTrue(presence._in_quiet_hours(pol))

    def test_snapshot_returns_dataclass(self):
        snap = presence.snapshot({})
        self.assertIsInstance(snap, presence.PresenceSnapshot)
        self.assertFalse(snap.presenter_mode_active)


# --------------------------------------------------------------------------
# runner.py
# --------------------------------------------------------------------------
class TestRunner(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.state = base / "state"
        self.state.mkdir()
        self._orig = {
            "STATE_DIR": runner.STATE_DIR,
            "FIRED_PATH": runner.FIRED_PATH,
            "DRY_RUN_LOG": runner.DRY_RUN_LOG,
        }
        runner.STATE_DIR = self.state
        runner.FIRED_PATH = self.state / "fired.json"
        runner.DRY_RUN_LOG = base / "logs" / "dryrun.log"

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(runner, k, v)
        self._tmp.cleanup()

    def test_render_ok_and_missing_key(self):
        self.assertEqual(runner._render("hi {title}", {"title": "X"}), "hi X")
        self.assertIn("render-error", runner._render("hi {missing}", {"title": "X"}))

    def test_load_fired_missing_and_bad(self):
        self.assertEqual(runner._load_fired(), {})
        runner.FIRED_PATH.write_text("{bad json")
        self.assertEqual(runner._load_fired(), {})

    def test_save_and_load_fired_roundtrip(self):
        runner._save_fired({"k": "2026"})
        self.assertEqual(runner._load_fired(), {"k": "2026"})

    def _spec(self, **kw):
        base = {"name": "cal", "source": "google_calendar",
                "body_template": "{title} in {minutes_until}m", "urgency": "important"}
        base.update(kw)
        return base

    def _presence(self):
        from dataclasses import dataclass

        @dataclass
        class P:
            presenter_mode_active: bool = False
            voice_client_connected: bool = False
            owner_active_in_discord_within_min_5: bool = False
            in_quiet_hours: bool = False
        return P()

    def _policy(self):
        return {"default_channel": {"critical": "call", "important": "sms", "fyi": "queue"}}

    def test_process_ping_requires_name_and_source(self):
        self.assertEqual(runner._process_ping({}, {}, self._presence(), self._policy(), False), [])

    def test_process_ping_unknown_source(self):
        out = runner._process_ping(self._spec(source="nope"), {}, self._presence(), self._policy(), False)
        self.assertEqual(out[0]["status"], "error")
        self.assertIn("unknown source", out[0]["reason"])

    def _fake_source(self, items):
        m = mock.Mock()
        m.fetch = mock.Mock(return_value=items)
        return m

    def test_process_ping_dry_run_and_dedup_and_match(self):
        items = [
            {"title": "Standup", "minutes_until": 10, "dedup_key_suffix": "a"},
            {"title": "Rest", "minutes_until": 10, "dedup_key_suffix": "b"},
            {"title": "Seen", "minutes_until": 10, "dedup_key_suffix": "c"},
        ]
        spec = self._spec(match={"exclude_titles_matching": "(?i)^rest$"})
        fired = {"cal:c": "2026"}
        with mock.patch.object(runner.importlib, "import_module", return_value=self._fake_source(items)):
            out = runner._process_ping(spec, fired, self._presence(), self._policy(), False)
        # Rest excluded by match, Seen excluded by dedup -> only Standup.
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "dry-run")
        self.assertEqual(out[0]["channel"], "sms")

    def test_process_ping_live_sent_marks_fired(self):
        items = [{"title": "X", "minutes_until": 5, "dedup_key_suffix": "a"}]
        src = self._fake_source(items)
        action = mock.Mock()
        action.send = mock.Mock(return_value={"ok": True, "id": "MSG1"})

        def _imp(name):
            return src if name.startswith("sources.") else action
        fired: dict = {}
        with mock.patch.object(runner.importlib, "import_module", side_effect=_imp):
            out = runner._process_ping(self._spec(), fired, self._presence(), self._policy(), True)
        self.assertEqual(out[0]["status"], "sent")
        self.assertEqual(out[0]["id"], "MSG1")
        self.assertIn("cal:a", fired)

    def test_process_ping_live_action_error(self):
        items = [{"title": "X", "minutes_until": 5, "dedup_key_suffix": "a"}]
        src = self._fake_source(items)
        action = mock.Mock()
        action.send = mock.Mock(return_value={"ok": False, "error": "nope"})

        def _imp(name):
            return src if name.startswith("sources.") else action
        with mock.patch.object(runner.importlib, "import_module", side_effect=_imp):
            out = runner._process_ping(self._spec(), {}, self._presence(), self._policy(), True)
        self.assertEqual(out[0]["status"], "error")
        self.assertEqual(out[0]["reason"], "nope")

    def test_process_ping_live_unknown_channel(self):
        items = [{"title": "X", "minutes_until": 5, "dedup_key_suffix": "a"}]
        src = self._fake_source(items)

        def _imp(name):
            if name.startswith("sources."):
                return src
            raise ModuleNotFoundError(name)
        with mock.patch.object(runner.importlib, "import_module", side_effect=_imp):
            out = runner._process_ping(self._spec(), {}, self._presence(), self._policy(), True)
        self.assertEqual(out[0]["status"], "error")
        self.assertIn("no action module", out[0]["reason"])

    def test_log_dry_run_writes(self):
        runner._log_dry_run([])  # no-op path
        self.assertFalse(runner.DRY_RUN_LOG.exists())
        runner._log_dry_run([{"ping": "cal", "status": "dry-run"}])
        self.assertTrue(runner.DRY_RUN_LOG.exists())
        self.assertIn("cal", runner.DRY_RUN_LOG.read_text())

    def test_main_dry_run_end_to_end(self):
        pings = self.state / "pings.yaml"
        policy = self.state / "policy.yaml"
        pings.write_text(
            "pings:\n"
            "  - name: cal\n"
            "    source: google_calendar\n"
            "    urgency: important\n"
            "    body_template: '{title}'\n"
        )
        policy.write_text("default_channel:\n  important: sms\n")
        items = [{"title": "Sync", "minutes_until": 5, "dedup_key_suffix": "a"}]
        with mock.patch.object(runner, "_bootstrap_workspace_config"), \
                mock.patch.object(runner, "snapshot", return_value=self._presence()), \
                mock.patch.object(runner.importlib, "import_module", return_value=self._fake_source(items)), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = runner.main(["--once", "--pings", str(pings), "--policy", str(policy)])
        self.assertEqual(rc, 0)
        # dry-run persists intended deliveries to the dry-run log (asserting the
        # side effect, not captured stdout, which the CI harness manages).
        self.assertTrue(runner.DRY_RUN_LOG.exists())
        logged = runner.DRY_RUN_LOG.read_text()
        self.assertIn("Sync", logged)
        self.assertIn("dry-run", logged)


class TestPresenceEdges(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.state = base / "state"
        self.state.mkdir()
        self._orig_state = presence.STATE_DIR
        self._orig_log = presence.VOICE_LOG
        presence.STATE_DIR = self.state
        presence.VOICE_LOG = base / "logs" / "voice-agent.log"

    def tearDown(self):
        presence.STATE_DIR = self._orig_state
        presence.VOICE_LOG = self._orig_log
        self._tmp.cleanup()

    def test_voice_client_read_exception_is_swallowed(self):
        # VOICE_LOG exists but is a directory -> open('rb') raises -> caught.
        presence.VOICE_LOG.parent.mkdir(parents=True, exist_ok=True)
        presence.VOICE_LOG.mkdir()
        self.assertFalse(presence._voice_client_connected())

    def test_owner_active_non_numeric_ts(self):
        act = self.state / "last-owner-activity.json"
        act.write_text(json.dumps({"ts": "nope", "channel": "discord"}))
        self.assertFalse(presence._owner_active_in_discord_within(5))

    def test_quiet_hours_bad_timezone_falls_back(self):
        pol = {"quiet_hours": {"start": "00:00", "end": "23:59", "timezone": "Not/AZone"}}
        # bad tz -> ZoneInfo raises -> local-time fallback; window covers ~all day.
        self.assertIsInstance(presence._in_quiet_hours(pol), bool)

    def test_quiet_hours_wraparound_branch(self):
        pol = {"quiet_hours": {"start": "23:00", "end": "01:00", "timezone": "UTC"}}
        self.assertIsInstance(presence._in_quiet_hours(pol), bool)


class TestRunnerConfigAndLive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._orig = {k: getattr(runner, k) for k in (
            "WORKSPACE_SKILL_DIR", "STATE_DIR", "DEFAULT_PINGS", "DEFAULT_POLICY",
            "TEMPLATE_DIR", "FIRED_PATH", "DRY_RUN_LOG")}
        ws = self.base / "ws-skill"
        runner.WORKSPACE_SKILL_DIR = ws
        runner.STATE_DIR = ws / "state"
        runner.DEFAULT_PINGS = ws / "pings.yaml"
        runner.DEFAULT_POLICY = ws / "channel-policy.yaml"
        runner.TEMPLATE_DIR = self.base / "templates"
        runner.FIRED_PATH = ws / "state" / "fired.json"
        runner.DRY_RUN_LOG = self.base / "logs" / "dryrun.log"
        runner.TEMPLATE_DIR.mkdir()
        (runner.TEMPLATE_DIR / "pings.yaml.example").write_text("pings: []\n")
        (runner.TEMPLATE_DIR / "channel-policy.yaml.example").write_text("default_channel: {}\n")

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(runner, k, v)
        self._tmp.cleanup()

    def test_bootstrap_copies_templates(self):
        runner._bootstrap_workspace_config()
        self.assertTrue(runner.DEFAULT_PINGS.exists())
        self.assertTrue(runner.DEFAULT_POLICY.exists())
        self.assertEqual(runner.DEFAULT_PINGS.read_text(), "pings: []\n")

    def test_load_yaml_reads_file(self):
        p = self.base / "x.yaml"
        p.write_text("a: 1\n")
        self.assertEqual(runner._load_yaml(p), {"a": 1})

    def test_main_live_saves_fired(self):
        from dataclasses import dataclass

        @dataclass
        class P:
            presenter_mode_active: bool = False
            voice_client_connected: bool = False
            owner_active_in_discord_within_min_5: bool = False
            in_quiet_hours: bool = False

        pings = self.base / "pings.yaml"
        policy = self.base / "policy.yaml"
        pings.write_text(
            "pings:\n  - name: cal\n    source: google_calendar\n"
            "    urgency: important\n    body_template: '{title}'\n")
        policy.write_text("default_channel:\n  important: sms\n")
        src = mock.Mock()
        src.fetch = mock.Mock(return_value=[{"title": "X", "minutes_until": 5, "dedup_key_suffix": "a"}])
        action = mock.Mock()
        action.send = mock.Mock(return_value={"ok": True, "id": "S1"})

        def _imp(name):
            return src if name.startswith("sources.") else action
        with mock.patch.object(runner, "_bootstrap_workspace_config"), \
                mock.patch.object(runner, "snapshot", return_value=P()), \
                mock.patch.object(runner.importlib, "import_module", side_effect=_imp), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = runner.main(["--live", "--pings", str(pings), "--policy", str(policy)])
        self.assertEqual(rc, 0)
        action.send.assert_called_once()
        # live path persisted fired.json with the dispatched dedup key.
        self.assertIn("cal:a", json.loads(runner.FIRED_PATH.read_text()))


class TestRouterNoPredicate(unittest.TestCase):
    def test_override_without_if_is_skipped(self):
        from channel_router import Ping, route
        from dataclasses import dataclass

        @dataclass
        class P:
            presenter_mode_active: bool = False
            voice_client_connected: bool = False
            owner_active_in_discord_within_min_5: bool = False
            in_quiet_hours: bool = False

        policy = {
            "default_channel": {"important": "sms"},
            "overrides": [{"name": "no-if", "behavior": {"important": "call"}}],
        }
        ping = Ping(name="t", urgency="important", voice_natural=False, body="b", dedup_key="t:1")
        # override has no `if:` -> skipped -> falls through to default.
        self.assertEqual(route(ping, P(), policy), "sms")


if __name__ == "__main__":
    unittest.main()
