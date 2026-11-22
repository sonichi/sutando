#!/usr/bin/env python3
"""The bridge carries the core's recovery record (core_recovery.v1) as `health.recovery` on its
heartbeat, through the reader the adapter injects: the body matches the contract fixtures, a
change beats early, and a missing, broken or refused record never costs the beat.

Run: python3 tests/gateway-core-recovery.test.py
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

FIXTURES = REPO / "contracts" / "core_recovery.v1"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def core_row(**kw):
    row = {"id": "core", "role": "core", "alive": True, "motion": "idle", "condition": "healthy",
           "reason": None, "since": None}
    return {"checked_at": 1.0, "agents": [{**row, **kw}]}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.record = None
        for name, value in (("_STATE", self.state), ("_heartbeat_retry_at", 0.0),
                            ("_last_heartbeat_at", 0.0), ("_last_core_health", None),
                            ("_core_recovery_refused_until", 0.0), ("_HEALTH_COMMAND", None),
                            ("_CORE_RECOVERY_READER", lambda: self.record)):
            p = mock.patch.object(rgb, name, value)
            p.start()
            self.addCleanup(p.stop)

    def beat(self, snap=None, force=False, req=None):
        sent_bodies = []

        def capture(method, path, body, timeout=None):
            sent_bodies.append(body)
            return req(body) if req else {}
        with mock.patch.object(rgb, "_health_snapshot", return_value=snap), \
                mock.patch.object(rgb, "_req", capture), mock.patch.object(rgb, "_log"):
            sent = rgb._post_heartbeat(set(), force=force)
        self.sent = sent_bodies
        return sent_bodies[-1] if sent and sent_bodies else None


class Contract(Base):
    """The captured POST body carries every field of the fixture with the fixture's value."""

    def assert_matches(self, name, snap):
        want = fixture(name)
        self.record = want["health"]["recovery"]
        if "step" in want:
            (self.state / "core-status.json").write_text(json.dumps({"status": want["status"], "step": want["step"]}))
        else:
            (self.state / "core-status.json").write_text(json.dumps({"status": want["status"]}))
        with mock.patch.object(rgb, "LOCAL_TIER", want["tier"]):
            body = self.beat(snap, force=True)
        for key, value in want.items():
            if key == "capabilities":
                self.assertLessEqual(set(value), set(body["capabilities"]), body["capabilities"])
            else:
                self.assertEqual(body.get(key), value, key)
        self.assertNotIn("step", body) if "step" not in want else None

    def test_the_claude_heartbeat_fixture(self):
        self.assert_matches("heartbeat.json", core_row(condition="abnormal", reason="usage-limit", since=1763730000))

    def test_the_codex_heartbeat_fixture(self):
        self.assert_matches("heartbeat.codex.json", core_row(condition="abnormal", reason="quota-limit",
                                                             since=1763730120))


class Carriage(Base):
    def test_the_record_rides_with_its_capability(self):
        self.record = {"v": 1, "state": "running", "actions": []}
        body = self.beat(core_row(), force=True)
        self.assertEqual(body["health"]["recovery"], self.record)
        self.assertIn(rgb.CORE_RECOVERY_CAPABILITY, body["capabilities"])
        self.assertIn("worker_health.v1", body["capabilities"])

    def test_no_record_means_no_field_and_no_capability(self):
        body = self.beat(core_row(), force=True)
        self.assertNotIn("recovery", body["health"])
        self.assertNotIn(rgb.CORE_RECOVERY_CAPABILITY, body["capabilities"])

    def test_a_standalone_sparrow_has_no_reader(self):
        self.record = {"v": 1, "state": "running"}
        with mock.patch.object(rgb, "_CORE_RECOVERY_READER", None):
            body = self.beat(None, force=True)
        self.assertNotIn("health", body)
        self.assertNotIn(rgb.CORE_RECOVERY_CAPABILITY, body["capabilities"])

    def test_without_a_health_row_the_record_still_rides(self):
        self.record = {"v": 1, "state": "crash_loop", "actions": ["restart_core"]}
        body = self.beat(None, force=True)
        self.assertEqual(body["health"], {"alive": None, "motion": "unknown", "recovery": self.record})

    def test_a_changed_record_beats_inside_the_interval(self):
        self.record = {"v": 1, "state": "running"}
        self.assertIsNotNone(self.beat(core_row(), force=True))
        self.assertIsNone(self.beat(core_row()), "nothing changed: the interval stands")
        self.record = {"v": 1, "state": "paused_limit"}
        self.assertEqual(self.beat(core_row())["health"]["recovery"]["state"], "paused_limit")

    def test_a_broken_or_oversized_record_never_costs_the_beat(self):
        def boom():
            raise OSError("unreadable")
        for reader in (boom, lambda: ["not", "a", "dict"], lambda: {"v": 1, "pad": "x" * rgb.CORE_RECOVERY_MAX_BYTES}):
            rgb._last_core_health = None
            with mock.patch.object(rgb, "_CORE_RECOVERY_READER", reader):
                body = self.beat(core_row(), force=True)
            self.assertIsNotNone(body, reader)
            self.assertNotIn("recovery", body["health"])


class RefusingBroker(Base):
    """A broker that rejects the unknown block still gets its liveness beat."""

    def refuse(self, code):
        def req(body):
            if "recovery" in (body.get("health") or {}):
                raise urllib.error.HTTPError("u", code, "bad", {}, io.BytesIO(b""))
            return {}
        return req

    def test_a_refused_block_is_resent_without_it_and_held_back(self):
        self.record = {"v": 1, "state": "running"}
        body = self.beat(core_row(), force=True, req=self.refuse(422))
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("recovery", body["health"])
        self.assertNotIn(rgb.CORE_RECOVERY_CAPABILITY, body["capabilities"])
        self.assertIn("worker_health.v1", body["capabilities"])
        self.assertGreater(rgb._core_recovery_refused_until, time.time())
        nxt = self.beat(core_row(), force=True, req=self.refuse(422))
        self.assertEqual((len(self.sent), "recovery" in nxt["health"]), (1, False), "held back, one POST")

    def test_a_refusal_with_only_the_record_drops_the_whole_health(self):
        self.record = {"v": 1, "state": "running"}
        body = self.beat(None, force=True, req=self.refuse(400))
        self.assertNotIn("health", body)
        self.assertNotIn("worker_health.v1", body["capabilities"])

    def test_other_errors_keep_their_old_handling(self):
        self.record = {"v": 1, "state": "running"}
        self.assertIsNone(self.beat(core_row(), force=True, req=self.refuse(500)))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(rgb._core_recovery_refused_until, 0.0)
        with self.assertRaises(urllib.error.HTTPError):
            self.beat(core_row(), force=True, req=self.refuse(401))


if __name__ == "__main__":
    unittest.main()
