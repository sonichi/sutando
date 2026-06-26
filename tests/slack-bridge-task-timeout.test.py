#!/usr/bin/env python3
"""Tests for _check_task_timeouts() in src/slack-bridge.py.

Guards the silent-fail fix: when the core session wedges (e.g. loops on the
1M-context usage-credit API error), no results/<task>.txt is ever written and
the Slack user just sees silence. The result_watcher now posts a one-time
"still working / may have hit a limit" reply after SLACK_TASK_TIMEOUT_SEC, and
KEEPS the pending entry so a late real result is still delivered.

Run: python3 tests/slack-bridge-task-timeout.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_bridge(workspace: Path):
    """Load slack-bridge.py with stubbed slack_bolt and a temp workspace."""
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-token"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-token"
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"  # v0.8: opt-in env-honor
    os.environ["SLACK_TASK_TIMEOUT_SEC"] = "600"

    sys.modules.pop("slack_bridge_timeout_under_test", None)

    class _StubApp:
        def __init__(self, *a, **kw):
            self.client = types.SimpleNamespace()
        def event(self, _name):
            return lambda fn: fn

    try:
        import slack_bolt as _bolt
        _bolt.App = _StubApp
    except ImportError:
        stub = types.ModuleType("slack_bolt")
        stub.App = _StubApp
        sys.modules["slack_bolt"] = stub

    for pkg in ("slack_bolt.adapter", "slack_bolt.adapter.socket_mode"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            if pkg.endswith("socket_mode"):
                m.SocketModeHandler = object
            sys.modules[pkg] = m

    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location(
        "slack_bridge_timeout_under_test", REPO / "src" / "slack-bridge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCheckTaskTimeouts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="slack-timeout-test-"))
        self.mod = _load_bridge(self.tmp)
        self.calls = []
        # Capture replies instead of hitting Slack.
        self.mod._send_reply = (
            lambda channel, thread_ts, text, task_id=None: self.calls.append(
                {"task_id": task_id, "channel": channel, "text": text}
            )
        )
        with self.mod.pending_replies_lock:
            self.mod.pending_replies.clear()

    def _seed(self, task_id, age_sec, **extra):
        info = {
            "channel": "D1",
            "thread_ts": None,
            "submitted_at": time.time() - age_sec,
            "timed_out": False,
        }
        info.update(extra)
        with self.mod.pending_replies_lock:
            self.mod.pending_replies[task_id] = info

    def test_stale_task_notified_fresh_left_alone(self):
        self._seed("task-old", 700)        # past 600s timeout
        self._seed("task-fresh", 10)       # well within
        self.mod._check_task_timeouts()
        notified = [c["task_id"] for c in self.calls]
        self.assertEqual(notified, ["task-old"])
        self.assertIn("usage-credit", self.calls[0]["text"])

    def test_notify_once_only(self):
        self._seed("task-old", 700)
        self.mod._check_task_timeouts()
        self.mod._check_task_timeouts()
        self.assertEqual(len(self.calls), 1, "should not re-notify a timed-out task")

    def test_entry_retained_for_late_result(self):
        self._seed("task-old", 700)
        self.mod._check_task_timeouts()
        with self.mod.pending_replies_lock:
            self.assertIn("task-old", self.mod.pending_replies)
            self.assertTrue(self.mod.pending_replies["task-old"]["timed_out"])

    def test_zero_disables(self):
        self.mod.TASK_TIMEOUT_SEC = 0
        self._seed("task-old", 9999)
        self.mod._check_task_timeouts()
        self.assertEqual(self.calls, [])

    def test_send_failure_retries(self):
        """Regression (PR #1428 review, blocker 1): a failed _send_reply must
        NOT mark the task timed_out, or one Slack hiccup silences the warning
        forever — recreating the silent no-op this watchdog exists to fix."""
        self._seed("task-old", 700)

        def boom(channel, thread_ts, text, task_id=None):
            raise RuntimeError("slack 500")

        self.mod._send_reply = boom
        self.mod._check_task_timeouts()
        with self.mod.pending_replies_lock:
            self.assertFalse(
                self.mod.pending_replies["task-old"]["timed_out"],
                "a failed send must leave timed_out unset so the next pass retries",
            )

        # Next pass with a working sender must retry and then mark it sent.
        self.mod._send_reply = (
            lambda channel, thread_ts, text, task_id=None: self.calls.append(
                {"task_id": task_id, "channel": channel, "text": text}
            )
        )
        self.mod._check_task_timeouts()
        self.assertEqual([c["task_id"] for c in self.calls], ["task-old"])
        with self.mod.pending_replies_lock:
            self.assertTrue(self.mod.pending_replies["task-old"]["timed_out"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
