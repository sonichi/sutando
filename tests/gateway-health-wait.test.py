#!/usr/bin/env python3
"""The dedicated health-check channel: the bridge holds GET /v1/health/wait open on its own
thread and answers each check_id at once with POST /v1/health/<id>, carrying the heartbeat's
fields and how long the task loop has gone without progress.

Run: python3 tests/gateway-health-wait.test.py
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile
import unittest
import urllib.error
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


class Stop:
    """Stands in for _HEALTH_STOP: records each backoff wait, never sleeps."""
    def __init__(self):
        self.waits, self.stopped = [], False

    def is_set(self):
        return self.stopped

    def wait(self, s):
        self.waits.append(s)
        return self.stopped

    def set(self):
        self.stopped = True


def http_error(code):
    return urllib.error.HTTPError("https://gw.example/relay", code, "x", {}, None)


class Channel(unittest.TestCase):
    def run_loop(self, answers, inflight=frozenset(), loop_idle=0.0, clock=None):
        """Feed the GETs from `answers` (a value, or an exception to raise); stop when they run out."""
        stop, calls, script = Stop(), [], list(answers)

        def req(method, path, payload=None, timeout=None):
            calls.append((method, path, payload, timeout))
            if method == "POST":
                return {}
            if not script:
                # Stop with a read timeout, which takes no pause, so `waits` holds only the script's own.
                stop.set()
                raise TimeoutError
            a = script.pop(0)
            if isinstance(a, BaseException):
                raise a
            return a

        tick = {"at": 1000.0 - loop_idle}
        with mock.patch.object(rgb, "_HEALTH_STOP", stop), \
                mock.patch.object(rgb, "_LOCK_LOST", mock.Mock(is_set=lambda: False)), \
                mock.patch.object(rgb, "_req", side_effect=req), \
                mock.patch.object(rgb, "_LOOP_TICK", tick), \
                mock.patch.object(rgb.time, "monotonic", **({"side_effect": clock} if clock else {"return_value": 1000.0})), \
                mock.patch.object(rgb, "_health_snapshot", return_value=None), \
                mock.patch.object(rgb, "_reported_core_status", return_value=("working", "Reviewing #5624")), \
                mock.patch.object(rgb, "_log"):
            rgb._health_wait_loop(set(inflight))
        return calls, stop.waits

    def test_a_check_is_answered_at_once_with_the_heartbeat_fields_and_the_task_loop_idle_time(self):
        calls, waits = self.run_loop([{"check_id": "chk-1"}], inflight={"t1", "t2"}, loop_idle=95)
        method, path, _, timeout = calls[0]
        self.assertEqual((method, path), ("GET", f"/v1/health/wait?wait={rgb.HEALTH_WAIT_S}"))
        self.assertEqual(timeout, rgb.HEALTH_WAIT_S + 10)
        method, path, body, _ = calls[1]
        self.assertEqual((method, path), ("POST", "/v1/health/chk-1"))
        self.assertEqual(body["check_id"], "chk-1")
        self.assertEqual(body["inflight"], 2)
        self.assertEqual((body["status"], body["step"]), ("working", "Reviewing #5624"))
        # A wedged task loop still answers here, and says how long it has been stuck.
        self.assertEqual(body["task_loop_idle_s"], 95)
        self.assertIn("health-wait", body["capabilities"])
        # The check came back at once, so the next wait is a second off.
        self.assertEqual(waits, [1])

    def test_an_empty_or_invalid_answer_reports_nothing(self):
        calls, waits = self.run_loop([{}, {"check_id": "../x"}, {"check_id": 7}, ["junk"]])
        self.assertFalse([c for c in calls if c[0] == "POST"])
        # Every answer came back at once, empty or malformed, so the loop paused each time instead of spinning.
        self.assertEqual(waits, [1, 1, 1, 1])

    def test_a_relay_answering_every_wait_at_once_is_paced_and_a_held_wait_is_not(self):
        _, waits = self.run_loop([{"check_id": "a"}, {"check_id": "b"}, {"check_id": "c"}])
        self.assertEqual(waits, [1, 1, 1])
        # Held for 30 s each, as a relay normally does: no pause on top.
        calls, waits = self.run_loop([{}, {"check_id": "a"}], clock=itertools.count(1000.0, 30.0))
        self.assertEqual(waits, [])
        self.assertEqual([c[1] for c in calls if c[0] == "POST"], ["/v1/health/a"])

    def test_a_relay_without_the_route_is_retried_on_a_doubling_backoff_and_picked_up_later(self):
        calls, waits = self.run_loop([http_error(404), http_error(405), http_error(404), {"check_id": "c"}])
        self.assertEqual(waits, [rgb.HEARTBEAT_INTERVAL, 2 * rgb.HEARTBEAT_INTERVAL, 4 * rgb.HEARTBEAT_INTERVAL, 1])
        self.assertEqual([c[1] for c in calls if c[0] == "POST"], ["/v1/health/c"])

    def test_errors_never_end_the_thread_and_a_read_timeout_is_just_another_wait(self):
        calls, waits = self.run_loop([TimeoutError(), http_error(503), urllib.error.URLError("down"),
                                      RuntimeError("bad json"), http_error(401), {"check_id": "c"}])
        self.assertEqual(waits, [1, 2, 4, 8, 1])
        self.assertEqual([c[1] for c in calls if c[0] == "POST"], ["/v1/health/c"])

    def test_a_failed_report_is_logged_and_the_channel_keeps_waiting(self):
        stop, gets = Stop(), [{"check_id": "a"}, {"check_id": "b"}]
        posted = []

        def req(method, path, payload=None, timeout=None):
            if method == "POST":
                posted.append(path)
                if path.endswith("/a"):
                    raise urllib.error.URLError("reset")
                return {}
            if not gets:
                stop.set()
                return {}
            return gets.pop(0)

        with mock.patch.object(rgb, "_HEALTH_STOP", stop), \
                mock.patch.object(rgb, "_LOCK_LOST", mock.Mock(is_set=lambda: False)), \
                mock.patch.object(rgb, "_req", side_effect=req), \
                mock.patch.object(rgb, "_health_snapshot", return_value=None), \
                mock.patch.object(rgb, "_reported_core_status", return_value=(None, None)), \
                mock.patch.object(rgb, "_log"):
            rgb._health_wait_loop(set())
        self.assertEqual(posted, ["/v1/health/a", "/v1/health/b"])

    def test_a_bridge_that_lost_the_poller_lock_stops_answering(self):
        with mock.patch.object(rgb, "_HEALTH_STOP", Stop()), \
                mock.patch.object(rgb, "_LOCK_LOST", mock.Mock(is_set=lambda: True)), \
                mock.patch.object(rgb, "_req") as req:
            rgb._health_wait_loop(set())
        req.assert_not_called()


class Heartbeat(unittest.TestCase):
    def test_the_heartbeat_advertises_the_channel_and_keeps_its_shape(self):
        with mock.patch.object(rgb, "_reported_core_status", return_value=("idle", None)):
            body = rgb._heartbeat_payload({"t"}, None)
        self.assertEqual(body["capabilities"], ["task-ack", "heartbeat", "result-skip-markers",
                                                "core-status", "team-collaborator", "health-wait"])
        self.assertEqual((body["inflight"], body["status"]), (1, "idle"))
        self.assertNotIn("step", body)
        self.assertNotIn("check_id", body)

    def test_main_starts_the_channel_beside_the_task_loop(self):
        src = (REPO / "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py").read_text()
        self.assertIn("    _outbound_thread = _start_outbound_worker(inflight)\n    _start_health_wait(inflight)\n", src)
        self.assertIn("_HEALTH_STOP.set()", src)


if __name__ == "__main__":
    unittest.main()
