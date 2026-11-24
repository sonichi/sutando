#!/usr/bin/env python3
"""The bridge carries the core's runtime profile (runtime_profile.v1) as the heartbeat's top-level
`runtime_profile`, through the reader the adapter injects: the body matches the contract fixtures,
and a missing, broken, oversized or refused profile never costs the beat.

Run: python3 tests/gateway-runtime-profile.test.py
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

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
_SANDBOX = tempfile.TemporaryDirectory()
os.environ["AGENT_CONNECT_STATE_DIR"] = str(Path(_SANDBOX.name) / "state")
os.environ["AGENT_CONNECT_TASK_DIR"] = str(Path(_SANDBOX.name) / "tasks")
os.environ["AGENT_CONNECT_RESULT_DIR"] = str(Path(_SANDBOX.name) / "results")
os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
os.environ["REMOTE_TASK_PROVIDER"] = "remote-gateway"
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
from ag2_sparrow import remote_gateway_bridge as rgb

FIXTURES = REPO / "contracts" / "runtime_profile.v1"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def core_row():
    return {"checked_at": 1.0, "agents": [{"id": "core", "role": "core", "alive": True, "motion": "idle",
                                           "condition": "healthy", "reason": None, "since": None}]}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.profile = None
        self.record = None
        for name, value in (("_STATE", self.state), ("_heartbeat_retry_at", 0.0),
                            ("_last_heartbeat_at", 0.0), ("_last_core_health", None),
                            ("_core_recovery_refused_until", 0.0), ("_runtime_profile_refused_until", 0.0),
                            ("_runtime_profile_checked_at", 0.0), ("_runtime_profile_sent", None),
                            ("_HEALTH_COMMAND", None), ("_core_recovery_logged", None),
                            ("_CORE_RECOVERY_READER", lambda: self.record),
                            ("_RUNTIME_PROFILE_READER", lambda: self.profile)):
            p = mock.patch.object(rgb, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)

    def beat(self, snap=None, force=True, req=None):
        sent_bodies = []

        def capture(method, path, body, timeout=None):
            sent_bodies.append(json.loads(json.dumps(body)))
            return req(body) if req else {}
        with mock.patch.object(rgb, "_health_snapshot", return_value=snap), \
                mock.patch.object(rgb, "_req", capture), mock.patch.object(rgb, "_log"):
            sent = rgb._post_heartbeat(set(), force=force)
        self.sent = sent_bodies
        return sent_bodies[-1] if sent and sent_bodies else None


class Contract(Base):
    """The captured POST body carries every field of each fixture with the fixture's value."""

    def assert_matches(self, case):
        want = fixture(f"heartbeat.{case}.json")
        self.profile = want["runtime_profile"]
        core_status = {"status": want["status"], **({"step": want["step"]} if "step" in want else {})}
        (self.state / "core-status.json").write_text(json.dumps(core_status))
        snap = core_row() if "worker_health.v1" in want["capabilities"] else None
        with mock.patch.object(rgb, "LOCAL_TIER", want["tier"]):
            body = self.beat(snap)
        for key, value in want.items():
            if key == "capabilities":
                self.assertLessEqual(set(value), set(body["capabilities"]), body["capabilities"])
            else:
                self.assertEqual(body.get(key), value, key)

    def test_the_claude_core_fixture(self):
        self.assert_matches("claude")

    def test_the_codex_core_fixture(self):
        self.assert_matches("codex")

    def test_the_limited_core_fixture(self):
        self.assert_matches("limited")


class Carriage(Base):
    def test_the_profile_rides_beside_health_and_recovery(self):
        self.profile = fixture("heartbeat.claude.json")["runtime_profile"]
        self.record = {"v": 1, "state": "running"}
        body = self.beat(core_row())
        self.assertEqual(body["runtime_profile"], self.profile)
        self.assertEqual(body["health"]["recovery"], self.record)
        for cap in (rgb.RUNTIME_PROFILE_CAPABILITY, rgb.CORE_RECOVERY_CAPABILITY, "worker_health.v1"):
            self.assertIn(cap, body["capabilities"])

    def test_quota_rides_health_quota_and_never_the_profile(self):
        """health.quota (quota_status.v1) is the heartbeat's one source of usage and limit state."""
        quota = {"v": 1, "five_hour": {"used_pct": 100.0, "resets_at": 1795190400},
                 "limit_state": "rejected", "resumes_at": 1795190400, "last_checked": 1795176000}
        self.profile = fixture("heartbeat.limited.json")["runtime_profile"]
        with mock.patch.object(rgb, "_QUOTA_STATUS_READER", lambda: quota), \
                mock.patch.object(rgb, "_quota_status_refused_until", 0.0):
            body = self.beat(core_row())
        self.assertEqual(body["health"]["quota"], quota)
        self.assertEqual(set(body["runtime_profile"]["plan"]), {"account", "name"})
        for cap in (rgb.RUNTIME_PROFILE_CAPABILITY, rgb.QUOTA_STATUS_CAPABILITY):
            self.assertIn(cap, body["capabilities"])

    def test_no_profile_means_no_field_and_no_capability(self):
        body = self.beat(core_row())
        self.assertNotIn("runtime_profile", body)
        self.assertNotIn(rgb.RUNTIME_PROFILE_CAPABILITY, body["capabilities"])

    def test_a_standalone_sparrow_has_no_reader(self):
        self.profile = fixture("heartbeat.codex.json")["runtime_profile"]
        with mock.patch.object(rgb, "_RUNTIME_PROFILE_READER", None):
            body = self.beat(None)
        self.assertNotIn("runtime_profile", body)
        self.assertNotIn(rgb.RUNTIME_PROFILE_CAPABILITY, body["capabilities"])

    def test_a_broken_or_oversized_profile_never_costs_the_beat(self):
        def boom():
            raise OSError("unreadable")
        oversized = {"v": 1, "pad": "x" * rgb.RUNTIME_PROFILE_MAX_BYTES}
        for reader in (boom, lambda: ["not", "a", "dict"], lambda: oversized):
            with mock.patch.object(rgb, "_RUNTIME_PROFILE_READER", reader):
                body = self.beat(core_row())
            self.assertIsNotNone(body, reader)
            self.assertNotIn("runtime_profile", body)
            self.assertNotIn(rgb.RUNTIME_PROFILE_CAPABILITY, body["capabilities"])

    def test_the_profile_is_read_only_when_a_beat_goes_out(self):
        calls = []
        with mock.patch.object(rgb, "_RUNTIME_PROFILE_READER", lambda: calls.append(1) or None):
            self.beat(core_row())
            self.assertIsNone(self.beat(core_row(), force=False), "inside the interval: no beat")
        self.assertEqual(len(calls), 1)

    def test_a_profile_only_change_sends_the_beat_early(self):
        self.profile = fixture("heartbeat.claude.json")["runtime_profile"]
        self.beat(core_row())
        check = getattr(rgb, "RUNTIME_PROFILE_CHECK_S", 15.0)
        rgb._runtime_profile_checked_at = time.time() - check - 1
        self.profile = dict(self.profile, reported_at=self.profile["reported_at"] + 30)
        self.assertIsNone(self.beat(core_row(), force=False), "only reported_at moved: no early beat")
        rgb._runtime_profile_checked_at = time.time() - check - 1
        switched = {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5", "runtime": "claude"}
        self.profile = dict(self.profile, model=switched)
        body = self.beat(core_row(), force=False)
        self.assertIsNotNone(body, "a model switch reaches clients before the next interval")
        self.assertEqual(body["runtime_profile"]["model"], switched)
        self.assertIsNone(self.beat(core_row(), force=False), "checked again only after the check interval")


class RefusingBroker(Base):
    """A broker that rejects the unknown block still gets its liveness beat."""

    def refuse(self, code):
        def req(body):
            if "runtime_profile" in body or "recovery" in (body.get("health") or {}):
                raise urllib.error.HTTPError("u", code, "bad", {}, io.BytesIO(b""))
            return {}
        return req

    def test_a_refused_profile_is_resent_without_it_and_held_back(self):
        self.profile = fixture("heartbeat.claude.json")["runtime_profile"]
        body = self.beat(core_row(), req=self.refuse(422))
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("runtime_profile", body)
        self.assertNotIn(rgb.RUNTIME_PROFILE_CAPABILITY, body["capabilities"])
        self.assertIn("worker_health.v1", body["capabilities"])
        self.assertEqual(body["health"]["alive"], True)
        self.assertGreater(rgb._runtime_profile_refused_until, time.time())
        nxt = self.beat(core_row(), req=self.refuse(422))
        self.assertEqual((len(self.sent), "runtime_profile" in nxt), (1, False), "held back, one POST")

    def test_a_receiver_that_refuses_only_the_profile_keeps_recovery(self):
        self.profile = fixture("heartbeat.claude.json")["runtime_profile"]
        self.record = {"v": 1, "state": "running"}

        def req(body):
            if "runtime_profile" in body:
                raise urllib.error.HTTPError("u", 422, "bad", {}, io.BytesIO(b""))
            return {}
        body = self.beat(core_row(), req=req)
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("runtime_profile", body)
        self.assertEqual(body["health"]["recovery"], self.record)
        self.assertIn(rgb.CORE_RECOVERY_CAPABILITY, body["capabilities"])
        self.assertEqual(rgb._core_recovery_refused_until, 0.0)
        self.assertGreater(rgb._runtime_profile_refused_until, time.time())

    def test_a_receiver_that_refuses_only_the_profile_keeps_quota(self):
        self.profile = fixture("heartbeat.claude.json")["runtime_profile"]
        quota = {"v": 1, "five_hour": {"used_pct": 42.0, "resets_at": 1795190400}, "limit_state": "allowed",
                 "last_checked": 1795176000}

        def req(body):
            if "runtime_profile" in body:
                raise urllib.error.HTTPError("u", 422, "bad", {}, io.BytesIO(b""))
            return {}
        with mock.patch.object(rgb, "_QUOTA_STATUS_READER", lambda: quota), \
                mock.patch.object(rgb, "_quota_status_refused_until", 0.0):
            body = self.beat(core_row(), req=req)
            self.assertEqual(rgb._quota_status_refused_until, 0.0)
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("runtime_profile", body)
        self.assertEqual(body["health"]["quota"], quota)
        self.assertIn(rgb.QUOTA_STATUS_CAPABILITY, body["capabilities"])

    def test_a_refusal_with_both_blocks_drops_both(self):
        self.profile = fixture("heartbeat.claude.json")["runtime_profile"]
        self.record = {"v": 1, "state": "running"}
        body = self.beat(None, req=self.refuse(400))
        self.assertNotIn("runtime_profile", body)
        self.assertNotIn("health", body)
        for cap in (rgb.RUNTIME_PROFILE_CAPABILITY, rgb.CORE_RECOVERY_CAPABILITY, "worker_health.v1"):
            self.assertNotIn(cap, body["capabilities"])
        self.assertGreater(rgb._runtime_profile_refused_until, time.time())
        self.assertGreater(rgb._core_recovery_refused_until, time.time())

    def test_other_errors_keep_their_old_handling(self):
        self.profile = fixture("heartbeat.codex.json")["runtime_profile"]
        self.assertIsNone(self.beat(core_row(), req=self.refuse(500)))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(rgb._runtime_profile_refused_until, 0.0)
        with self.assertRaises(urllib.error.HTTPError):
            self.beat(core_row(), req=self.refuse(401))


if __name__ == "__main__":
    unittest.main()
