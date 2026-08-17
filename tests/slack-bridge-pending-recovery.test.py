#!/usr/bin/env python3
"""Crash-recovery tests for Slack pending reply routing."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def _load_bridge(workspace: Path):
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-token"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-token"
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"

    class _StubApp:
        def __init__(self, *args, **kwargs):
            self.client = types.SimpleNamespace()

        def event(self, _name):
            return lambda fn: fn

    slack_bolt = types.ModuleType("slack_bolt")
    slack_bolt.App = _StubApp
    sys.modules["slack_bolt"] = slack_bolt
    sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
    socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket_mode.SocketModeHandler = object
    sys.modules["slack_bolt.adapter.socket_mode"] = socket_mode
    sys.path.insert(0, str(REPO / "src"))

    name = f"slack_bridge_pending_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(name, REPO / "src" / "slack-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSlackPendingRecovery(unittest.TestCase):
    def test_pending_route_survives_restart_and_is_removed_after_delivery(self):
        with tempfile.TemporaryDirectory(prefix="slack-pending-") as raw:
            workspace = Path(raw)
            first = _load_bridge(workspace)
            task_id = f"task-{int(time.time() * 1000)}"
            route = {
                "channel": "C123",
                "thread_ts": "1784559002.847939",
                "access_tier": "owner",
                "submitted_at": time.time(),
                "timed_out": False,
            }
            first._set_pending_reply(task_id, route)

            state_file = workspace / "state" / "slack-pending-replies.json"
            self.assertEqual(json.loads(state_file.read_text())[task_id], route)

            restarted = _load_bridge(workspace)
            self.assertEqual(restarted.pending_replies[task_id], route)
            self.assertEqual(restarted._pop_pending_reply(task_id), route)
            self.assertNotIn(task_id, json.loads(state_file.read_text()))

    def test_route_is_written_before_task_and_rolled_back_on_write_failure(self):
        with tempfile.TemporaryDirectory(prefix="slack-pending-write-") as raw:
            workspace = Path(raw)
            bridge = _load_bridge(workspace)
            task_id = f"task-{int(time.time() * 1000)}"
            route = {"channel": "D123", "thread_ts": None}
            task_file = workspace / "tasks" / f"{task_id}.txt"
            task_file.parent.mkdir(exist_ok=True)

            bridge._write_routed_task(task_file, "task body", task_id, route)
            written = task_file.read_text()
            # The route write now carries the #3014 envelope, so exact-equality
            # would pin the absence of a stamp. Body intact + verifies is stronger.
            from task_envelope import verify_text
            self.assertIn("task body", written)
            self.assertEqual(verify_text(written, workspace)["verdict"], "verified")
            self.assertIn(task_id, bridge.pending_replies)

            failing_id = f"task-{int(time.time() * 1000) + 1}"
            with self.assertRaises(IsADirectoryError):
                bridge._write_routed_task(workspace, "cannot write", failing_id, route)
            self.assertNotIn(failing_id, bridge.pending_replies)

    def test_route_write_survives_a_raising_stamper(self):
        # Fail-open is the contract: a stamping error costs the stamp, never the
        # task. Without the guard the raise escapes and the task is lost.
        with tempfile.TemporaryDirectory(prefix="slack-pending-stamp-") as raw:
            workspace = Path(raw)
            bridge = _load_bridge(workspace)
            task_id = f"task-{int(time.time() * 1000)}"
            route = {"channel": "D123", "thread_ts": None}
            task_file = workspace / "tasks" / f"{task_id}.txt"
            task_file.parent.mkdir(exist_ok=True)

            import task_envelope

            original = task_envelope.stamp_text

            def boom(*_args, **_kwargs):
                raise RuntimeError("keychain on fire")

            task_envelope.stamp_text = boom
            try:
                bridge._write_routed_task(task_file, "task body", task_id, route)
            finally:
                task_envelope.stamp_text = original

            written = task_file.read_text()
            self.assertIn("task body", written)
            self.assertNotIn("envelope_hmac:", written)
            self.assertIn(task_id, bridge.pending_replies)

    def test_entries_older_than_seven_days_are_aged_out(self):
        with tempfile.TemporaryDirectory(prefix="slack-pending-old-") as raw:
            workspace = Path(raw)
            state_dir = workspace / "state"
            state_dir.mkdir()
            old_ms = int((time.time() - 8 * 86400) * 1000)
            state_file = state_dir / "slack-pending-replies.json"
            state_file.write_text(json.dumps({
                f"task-{old_ms}": {
                    "channel": "D123",
                    "thread_ts": None,
                    "submitted_at": old_ms / 1000,
                    "timed_out": False,
                }
            }))

            restarted = _load_bridge(workspace)
            self.assertEqual(restarted.pending_replies, {})
            self.assertEqual(json.loads(state_file.read_text()), {})

    def test_invalid_state_is_sanitized_without_crashing(self):
        with tempfile.TemporaryDirectory(prefix="slack-pending-invalid-") as raw:
            workspace = Path(raw)
            state_dir = workspace / "state"
            state_dir.mkdir()
            state_file = state_dir / "slack-pending-replies.json"

            state_file.write_text("[]")
            self.assertEqual(_load_bridge(workspace).pending_replies, {})

            malformed_id = "task-not-a-timestamp"
            state_file.write_text(json.dumps({
                "task-123": "not-a-route",
                malformed_id: {"channel": "D123", "thread_ts": None},
            }))
            loaded = _load_bridge(workspace).pending_replies
            self.assertNotIn("task-123", loaded)
            self.assertIn(malformed_id, loaded)

            state_file.write_text("{broken json")
            self.assertEqual(_load_bridge(workspace).pending_replies, {})

    def test_defensive_write_and_missing_timeout_entry_do_not_raise(self):
        with tempfile.TemporaryDirectory(prefix="slack-pending-defensive-") as raw:
            workspace = Path(raw)
            bridge = _load_bridge(workspace)
            bridge.PENDING_REPLIES_FILE = workspace  # replace(directory) must fail
            bridge._atomic_write_pending_replies({"task-1": {"channel": "D1"}})
            bridge._mark_pending_timed_out("task-missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
