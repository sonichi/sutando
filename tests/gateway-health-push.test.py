#!/usr/bin/env python3
"""The bridge carries the health snapshot to the broker: the core's row on the heartbeat
(with the worker_health.v1 capability), each worker's row and the pool suspension on the
workers report, and the identity it signed in as on its own gateway-status sidecar.

Run: python3 tests/gateway-health-push.test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_SANDBOX = tempfile.TemporaryDirectory()
os.environ["AGENT_CONNECT_STATE_DIR"] = str(Path(_SANDBOX.name) / "state")
os.environ["AGENT_CONNECT_TASK_DIR"] = str(Path(_SANDBOX.name) / "tasks")
os.environ["AGENT_CONNECT_RESULT_DIR"] = str(Path(_SANDBOX.name) / "results")
os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
from ag2_sparrow import remote_gateway_bridge as rgb  # noqa: E402

W1, W2 = "a" * 32, "b" * 32
ME = "@mark-desktop.agent:ag2.space"


def agent(id_, role, **kw):
    row = {"id": id_, "role": role, "label": None, "session": f"s-{id_}", "alive": True,
           "motion": "idle", "condition": "healthy", "reason": None, "since": None}
    return {**row, **kw}


def snap(core=None, workers=(), suspended=None):
    return {"checked_at": 1.0, "instance": ME, "overall": "ok", "suspended": suspended,
            "agents": ([core] if core else []) + list(workers)}


REPORT = {"ts": 1, "roster_version": 3,
          "workers": [{"id": W1, "state": "live"}, {"id": W2, "state": "retired"}, "junk"],
          "applied": {"labels": {}, "bindings": {}}}
LEGACY = {"ts": 1, "live_cores": [W1], "dead_cores": []}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        (self.ws / "state").mkdir()
        for name, value in (("_STATE", self.ws / "state"), ("_heartbeat_disabled", False),
                            ("_last_heartbeat_at", 0.0), ("_last_core_health", None),
                            ("_workers_pushed_identity", ""), ("_workers_push_retry_at", 0.0),
                            ("_workers_pushed_at", None),
                            ("_health_cache", {"at": None, "value": None, "error": None})):
            p = mock.patch.object(rgb, name, value)
            p.start()
            self.addCleanup(p.stop)


class Snapshot(Base):
    def test_the_snapshot_is_the_workspace_one_and_is_reused_within_the_ttl(self):
        (self.ws / "state" / "pool-suspended").write_text(json.dumps({"reason": "app-quit", "at": 7}))
        first = rgb._health_snapshot()
        self.assertEqual(first["suspended"], {"reason": "app-quit", "at": 7})
        self.assertEqual(first["agents"][0]["role"], "core")
        (self.ws / "state" / "pool-suspended").unlink()
        self.assertIs(rgb._health_snapshot(), first)
        with mock.patch.object(rgb.time, "monotonic", return_value=rgb._health_cache["at"] + rgb.HEALTH_TTL_S):
            self.assertIsNone(rgb._health_snapshot()["suspended"])

    def test_no_monorepo_src_means_no_health(self):
        with mock.patch.object(rgb, "_monorepo_src", return_value=""):
            self.assertIsNone(rgb._health_snapshot())

    def test_a_failing_snapshot_is_no_health_and_is_logged_once_per_error(self):
        logs = []
        with mock.patch.object(rgb, "_monorepo_src", side_effect=OSError("gone")), \
                mock.patch.object(rgb, "_log", logs.append), \
                mock.patch.object(rgb, "HEALTH_TTL_S", 0.0):
            self.assertIsNone(rgb._health_snapshot())
            self.assertIsNone(rgb._health_snapshot())
        self.assertEqual(logs, ["health snapshot unavailable: OSError: gone"])


class Rows(unittest.TestCase):
    def test_a_row_carries_only_the_health_fields_and_a_slug_reason(self):
        row = rgb._health_row(agent("core", "core", reason="Needs_Login!", session="x", label="y"))
        self.assertEqual(set(row), set(rgb.HEALTH_FIELDS))
        self.assertEqual(row["reason"], "needs-login")
        self.assertEqual(rgb._health_row(agent("core", "core", reason="x" * 60))["reason"], "x" * 40)
        self.assertIsNone(rgb._health_row(agent("core", "core", reason="!!!"))["reason"])
        self.assertIsNone(rgb._health_row(agent("core", "core"))["reason"])
        cut = rgb._health_row(agent("core", "core", reason="a" * 39 + " b"))["reason"]
        self.assertEqual(cut, "a" * 39)

    def test_core_health_is_the_core_row_or_none(self):
        self.assertIsNone(rgb._core_health(None))
        self.assertIsNone(rgb._core_health(snap(workers=[agent(W1, "worker")])))
        self.assertEqual(rgb._core_health(snap(core=agent("core", "core", motion="moving")))["motion"], "moving")

    def test_the_report_gains_each_worker_row_and_the_suspension(self):
        s = snap(core=agent("core", "core"), workers=[agent(W1, "worker", condition="abnormal", reason="offline")],
                 suspended={"reason": "app-quit", "at": 7})
        body = rgb._with_health(REPORT, s)
        self.assertEqual(body["workers"][0]["health"]["reason"], "offline")
        self.assertNotIn("health", body["workers"][1])
        self.assertEqual(body["workers"][2], "junk")
        self.assertEqual(body["suspended"], {"reason": "app-quit", "at": 7})
        self.assertNotIn("health", REPORT["workers"][0])

    def test_a_suspension_leaves_as_a_slug_and_a_numeric_time_only(self):
        self.assertIsNone(rgb._suspended_row(None))
        free_text = {"reason": "App Quit at /Users/mark (host)" + "x" * 60, "at": "yesterday"}
        row = rgb._suspended_row(free_text)
        self.assertRegex(row["reason"], r"^[a-z0-9-]{1,40}$")
        self.assertIsNone(row["at"])
        self.assertEqual(rgb._suspended_row({"reason": "!!!", "at": 5}), {"reason": "suspended", "at": 5})
        self.assertEqual(rgb._suspended_row({"reason": True, "at": True}), {"reason": "true", "at": None})

    def test_the_legacy_body_and_a_missing_snapshot_pass_through(self):
        self.assertIs(rgb._with_health(LEGACY, snap()), LEGACY)
        self.assertIs(rgb._with_health(REPORT, None), REPORT)


class WorkersPush(Base):
    def push(self, s):
        calls = []
        with mock.patch.object(rgb, "_health_snapshot", return_value=s), \
                mock.patch.object(rgb, "_req", lambda *a, **k: calls.append(a) or {}):
            rgb._maybe_push_workers_snapshot(("adv-1", {"workers": LEGACY, "report": REPORT}))
        return calls

    def test_a_health_change_re_pushes_the_same_advertisement(self):
        healthy = snap(workers=[agent(W1, "worker")])
        sick = snap(workers=[agent(W1, "worker", condition="abnormal", reason="needs-login")])
        self.assertEqual(self.push(healthy)[0][2]["workers"][0]["health"]["condition"], "healthy")
        self.assertEqual(self.push(healthy), [])
        self.assertEqual(self.push(sick)[0][2]["workers"][0]["health"]["reason"], "needs-login")

    def test_without_health_the_advertisement_alone_is_the_change_signal(self):
        self.assertIs(self.push(None)[0][2], REPORT)
        self.assertEqual(rgb._workers_pushed_identity, "adv-1")


class Heartbeat(Base):
    def beat(self, s, force=False):
        with mock.patch.object(rgb, "_health_snapshot", return_value=s), mock.patch.object(rgb, "_req") as req:
            sent = rgb._post_heartbeat(set(), force=force)
        return req.call_args.args[2] if sent else None

    def test_the_core_row_and_capability_ride_the_heartbeat(self):
        body = self.beat(snap(core=agent("core", "core", motion="moving")))
        self.assertEqual(body["health"], {"alive": True, "motion": "moving", "condition": "healthy",
                                          "reason": None, "since": None})
        self.assertIn("worker_health.v1", body["capabilities"])

    def test_a_change_beats_early_and_no_change_waits_for_the_interval(self):
        self.assertIsNotNone(self.beat(snap(core=agent("core", "core"))))
        self.assertIsNone(self.beat(snap(core=agent("core", "core"))))
        sick = snap(core=agent("core", "core", condition="abnormal", reason="needs-login"))
        self.assertEqual(self.beat(sick)["health"]["reason"], "needs-login")

    def test_without_health_the_heartbeat_is_unchanged(self):
        body = self.beat(None, force=True)
        self.assertNotIn("health", body)
        self.assertNotIn("worker_health.v1", body["capabilities"])


class SignedInIdentity(Base):
    def emit(self, connected):
        path = self.ws / "state" / "gateway-status.json"
        with mock.patch.object(rgb, "GATEWAY_STATUS_FILE", path), \
                mock.patch.object(rgb, "_reenroll_identity", return_value=ME):
            rgb._emit_gateway_status(connected)
        return json.loads(path.read_text())

    def test_the_sidecar_names_the_identity_only_while_connected(self):
        self.assertEqual(self.emit(True)["agent_id"], ME)
        self.assertIsNone(self.emit(False)["agent_id"])

    def test_the_snapshot_reads_it_back_as_the_instance(self):
        self.emit(True)
        sys.path.insert(0, str(REPO / "src"))
        import health_snapshot
        self.assertEqual(health_snapshot.snapshot(self.ws)["instance"], ME)


if __name__ == "__main__":
    unittest.main()
