#!/usr/bin/env python3
"""quota_status.v1: the core's Claude quota on the gateway heartbeat as `health.quota`, for the
s.space agent sidebar. Only the allowed fields leave; another login's numbers never do; the block
changes on a one-point move, a limit flip or after five minutes; a failure never costs the beat.

Run: python3 tests/quota-status.test.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
_SANDBOX = tempfile.TemporaryDirectory()
os.environ["AGENT_CONNECT_STATE_DIR"] = str(Path(_SANDBOX.name) / "state")
os.environ["AGENT_CONNECT_TASK_DIR"] = str(Path(_SANDBOX.name) / "tasks")
os.environ["AGENT_CONNECT_RESULT_DIR"] = str(Path(_SANDBOX.name) / "results")
os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
os.environ["REMOTE_TASK_PROVIDER"] = "remote-gateway"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
import quota_status as qs  # noqa: E402
from ag2_sparrow import remote_gateway_bridge as rgb  # noqa: E402

CONTRACT = json.loads((REPO / "contracts" / "quota_status.v1" / "heartbeat.json").read_text())
NOW = 1795176000.0
TOP_KEYS = {"v", "five_hour", "seven_day", "limit_state", "resumes_at", "last_checked"}
WINDOW_KEYS = {"used_pct", "resets_at"}


def record(**kw):
    """A full core record, with every field that must stay home."""
    rec = {
        "available": False, "last_checked": "2026-11-20T12:00:00.000Z", "source": "statusline",
        "headers": {"anthropic-ratelimit-unified-status": "rejected",
                    "anthropic-ratelimit-unified-5h-utilization": "1",
                    "anthropic-ratelimit-unified-5h-reset": "1795190400"},
        "recent_rejections": [{"ts": "2026-11-20T12:00:00.000Z", "status": 429, "snippet": "secret text"}],
        "last_request": {"model": "claude-opus-5-5", "at": "2026-11-20T12:00:00.000Z"},
        "credential_state": "ok", "credential_state_detail": "detail", "credential_state_at": "x",
        "utilization_5h": 1.0, "utilization_7d": 0.07,
        "resets_at_5h": "2026-11-20T16:00:00.000Z", "resets_at_7d": "2026-11-25T09:46:40.000Z",
        "limit_state": "rejected", "exhausted_since": "2026-11-20T12:00:00.000Z",
        "rejected_by": "stopfailure", "rejected_until": 1795190400.0,
        "account": "acct-core",
        "accounts": {"acct-core": {}, "acct-other": {"utilization_5h": 0.99, "limit_state": "rejected"}},
    }
    rec.update(kw)
    return rec


class Projection(unittest.TestCase):
    def test_only_the_allowed_fields_leave(self):
        block = qs.project(record(), NOW)
        self.assertLessEqual(set(block), TOP_KEYS)
        for name in ("five_hour", "seven_day"):
            self.assertEqual(set(block[name]), WINDOW_KEYS)
        text = json.dumps(block)
        for leak in ("acct", "secret", "claude-opus", "detail", "stopfailure", "headers"):
            self.assertNotIn(leak, text)

    def test_the_contract_fixture_is_what_the_record_projects_to(self):
        self.assertEqual(qs.project(record(), NOW), CONTRACT["health"]["quota"])

    def test_allowed_has_no_resumes_at(self):
        block = qs.project(record(limit_state="allowed", utilization_5h=0.42), NOW)
        self.assertEqual(block["limit_state"], "allowed")
        self.assertNotIn("resumes_at", block)
        self.assertEqual(block["five_hour"], {"used_pct": 42.0, "resets_at": 1795190400})

    def test_a_proxy_record_without_top_level_numbers_reads_its_headers(self):
        rec = {"headers": {"anthropic-ratelimit-unified-status": "allowed",
                           "anthropic-ratelimit-unified-7d-utilization": "0.25",
                           "anthropic-ratelimit-unified-7d-reset": "1795600000"},
               "last_checked": "2026-11-20T12:00:00.000Z"}
        self.assertEqual(qs.project(rec, NOW), {"v": 1, "seven_day": {"used_pct": 25.0, "resets_at": 1795600000},
                                                "limit_state": "allowed", "last_checked": 1795176000})

    def test_a_limit_with_no_stamp_resumes_at_the_fullest_window_reset(self):
        rec = record(rejected_until=None)
        self.assertEqual(qs.project(rec, NOW)["resumes_at"], 1795190400)

    def test_no_window_no_block(self):
        self.assertIsNone(qs.project({"limit_state": "allowed"}, NOW))
        self.assertIsNone(qs.project(None, NOW))


class AnotherLogin(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        (self.ws / "state").mkdir()

    def write(self, rec):
        (self.ws / "state" / "quota-state.json").write_text(json.dumps(rec))

    def test_another_logins_record_is_never_published(self):
        self.write(record(account="acct-other"))
        with mock.patch("claude_account.core_account", return_value="acct-core"):
            self.assertIsNone(qs.read(self.ws, now=NOW))

    def test_the_core_logins_record_publishes_its_own_numbers_not_the_accounts_map(self):
        self.write(record(limit_state="allowed", utilization_5h=0.1))
        with mock.patch("claude_account.core_account", return_value="acct-core"):
            block = qs.read(self.ws, now=NOW)
        self.assertEqual(block["five_hour"]["used_pct"], 10.0)
        self.assertNotIn("99", json.dumps(block))

    def test_a_codex_core_has_no_claude_quota(self):
        self.write(record())
        with mock.patch("claude_account.core_account", return_value="acct-core"):
            self.assertIsNone(qs.read(self.ws, runtime="codex", now=NOW))

    def test_a_missing_or_broken_file_is_none(self):
        self.assertIsNone(qs.read(self.ws, now=NOW))
        (self.ws / "state" / "quota-state.json").write_text("{ nope")
        self.assertIsNone(qs.read(self.ws, now=NOW))


def block(pct5=40.0, state="allowed", checked=1):
    return {"v": 1, "five_hour": {"used_pct": pct5, "resets_at": 1}, "limit_state": state, "last_checked": checked}


class Debounce(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.value = block()
        self.reads = 0

        def reader():
            self.reads += 1
            if isinstance(self.value, Exception):
                raise self.value
            return self.value
        self.d = qs.Debounced(reader, clock=lambda: self.now)

    def test_under_a_point_holds_the_last_block(self):
        self.assertEqual(self.d(), block())
        self.value = block(40.9, checked=2)
        self.now += 60
        self.assertEqual(self.d(), block())

    def test_a_point_publishes(self):
        self.d()
        self.value = block(41.0)
        self.assertEqual(self.d()["five_hour"]["used_pct"], 41.0)

    def test_a_limit_flip_publishes(self):
        self.d()
        self.value = block(40.2, state="rejected")
        self.assertEqual(self.d()["limit_state"], "rejected")

    def test_five_minutes_refresh_the_held_block(self):
        self.d()
        self.value = block(40.3, checked=9)
        self.now += 299
        self.assertEqual(self.d()["last_checked"], 1)
        self.now += 1
        self.assertEqual(self.d()["last_checked"], 9)

    def test_a_failed_read_keeps_the_last_block(self):
        self.d()
        self.value = OSError("unreadable")
        self.assertEqual(self.d(), block())

    def test_a_record_that_disappears_publishes_none(self):
        self.d()
        self.value = None
        self.assertIsNone(self.d())


def core_row():
    return {"checked_at": 1.0, "agents": [{"id": "core", "role": "core", "alive": True, "motion": "idle",
                                           "condition": "healthy", "reason": None, "since": None}]}


class Heartbeat(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.block = CONTRACT["health"]["quota"]
        for name, value in (("_STATE", Path(tmp.name)), ("_heartbeat_retry_at", 0.0),
                            ("_last_heartbeat_at", 0.0), ("_last_core_health", None),
                            ("_core_recovery_refused_until", 0.0), ("_quota_status_refused_until", 0.0),
                            ("_HEALTH_COMMAND", None), ("_core_recovery_logged", None),
                            ("_CORE_RECOVERY_READER", None),
                            ("_QUOTA_STATUS_READER", lambda: self.block)):
            p = mock.patch.object(rgb, name, value)
            p.start()
            self.addCleanup(p.stop)

    def beat(self, snap=None, force=False, req=None):
        self.sent = []

        def capture(method, path, body, timeout=None):
            self.sent.append(body)
            return req(body) if req else {}
        with mock.patch.object(rgb, "_health_snapshot", return_value=snap), \
                mock.patch.object(rgb, "_req", capture), mock.patch.object(rgb, "_log"):
            ok = rgb._post_heartbeat(set(), force=force)
        return self.sent[-1] if ok and self.sent else None

    def test_the_block_rides_as_the_contract_says(self):
        body = self.beat(core_row(), force=True)
        self.assertEqual(body["health"], CONTRACT["health"])
        self.assertIn(rgb.QUOTA_STATUS_CAPABILITY, body["capabilities"])

    def test_a_failing_or_oversized_reader_never_costs_the_beat(self):
        def boom():
            raise OSError("unreadable")
        for reader in (boom, lambda: "nope", lambda: {"pad": "x" * rgb.QUOTA_STATUS_MAX_BYTES}):
            rgb._last_core_health = None
            with mock.patch.object(rgb, "_QUOTA_STATUS_READER", reader):
                body = self.beat(core_row(), force=True)
            self.assertIsNotNone(body)
            self.assertNotIn("quota", body["health"])
            self.assertNotIn(rgb.QUOTA_STATUS_CAPABILITY, body["capabilities"])

    def test_a_refused_block_is_resent_without_it_and_held_back(self):
        def refuse(body):
            if "quota" in (body.get("health") or {}):
                raise urllib.error.HTTPError("u", 422, "bad", {}, io.BytesIO(b""))
            return {}
        body = self.beat(core_row(), force=True, req=refuse)
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("quota", body["health"])
        self.assertNotIn(rgb.QUOTA_STATUS_CAPABILITY, body["capabilities"])
        self.assertGreater(rgb._quota_status_refused_until, time.time())
        self.assertEqual(rgb._core_recovery_refused_until, 0.0)
        nxt = self.beat(core_row(), force=True, req=refuse)
        self.assertEqual((len(self.sent), "quota" in nxt["health"]), (1, False), "held back, one POST")

    def test_a_held_block_does_not_beat_early_and_a_moved_one_does(self):
        clock = [1000.0]
        value = [block()]
        d = qs.Debounced(lambda: value[0], clock=lambda: clock[0])
        with mock.patch.object(rgb, "_QUOTA_STATUS_READER", d):
            self.assertIsNotNone(self.beat(core_row(), force=True))
            value[0] = block(40.5, checked=2)
            self.assertIsNone(self.beat(core_row()), "under a point: the interval stands")
            value[0] = block(42.0)
            self.assertEqual(self.beat(core_row())["health"]["quota"]["five_hour"]["used_pct"], 42.0)

    def test_a_standalone_sparrow_has_no_reader(self):
        with mock.patch.object(rgb, "_QUOTA_STATUS_READER", None):
            body = self.beat(None, force=True)
        self.assertNotIn("health", body)


class Switch(unittest.TestCase):
    MANIFEST = REPO / "skills" / "quota-tracker" / "manifest.json"

    def test_on_by_default_and_off_by_env(self):
        import skill_config
        self.assertTrue(skill_config.enabled("QUOTA_STATUS_PUBLISH", self.MANIFEST, environ={}))
        self.assertFalse(skill_config.enabled("QUOTA_STATUS_PUBLISH", self.MANIFEST,
                                              environ={"QUOTA_STATUS_PUBLISH": "false"}))


if __name__ == "__main__":
    unittest.main()
