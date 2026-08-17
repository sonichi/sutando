#!/usr/bin/env python3
"""#3014 writer census: telegram and slack stamp the HMAC envelope at their edges.

Both write with a bare `write_text` rather than sparrow's `write_task_file`, so
the `set_task_stamper()` seam never reaches them — they need an edge stamp, the
shape discord-bridge and cron-runner use.

Slack is tested through its real central writer (`_write_routed_task`).
Telegram's write sits inside the long-polling message handler with no existing
harness, so its wiring is pinned at source level and the SEMANTICS (stamp +
fail-open) are covered behaviourally by the slack cases — the same split #3014
used for its own gateway wrapper.
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


def _load_slack(workspace: Path):
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-token"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-token"
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"

    class _StubApp:
        def __init__(self, *a, **k):
            self.client = types.SimpleNamespace()

        def event(self, _name):
            return lambda fn: fn

    slack_bolt = types.ModuleType("slack_bolt")
    slack_bolt.App = _StubApp
    sys.modules["slack_bolt"] = slack_bolt
    sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
    sm = types.ModuleType("slack_bolt.adapter.socket_mode")
    sm.SocketModeHandler = object
    sys.modules["slack_bolt.adapter.socket_mode"] = sm
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location(
        f"slack_env_{time.time_ns()}", REPO / "src" / "slack-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SlackEdgeStamp(unittest.TestCase):
    def test_route_write_is_stamped_and_verifies(self):
        with tempfile.TemporaryDirectory(prefix="slack-stamp-") as raw:
            ws = Path(raw)
            bridge = _load_slack(ws)
            from task_envelope import verify_text
            tid = f"task-{int(time.time() * 1000)}"
            tf = ws / "tasks" / f"{tid}.txt"
            tf.parent.mkdir(parents=True, exist_ok=True)
            bridge._write_routed_task(tf, f"id: {tid}\ntask: hello\n", tid,
                                      {"channel": "D1", "thread_ts": None})
            written = tf.read_text()
            self.assertEqual(verify_text(written, ws)["verdict"], "verified")
            self.assertTrue(written.splitlines()[0].startswith("id:"),
                            "the stamp goes AFTER id:, so task-last readers see a header")
            self.assertTrue(written.rstrip().endswith("task: hello"),
                            "task: stays last and the body is unchanged")
            # The key must land beside the tasks it signs, not via a second
            # independent workspace resolution inside task_envelope.
            self.assertTrue((ws / "state" / "auth" / "task-hmac.key").is_file())

    def test_write_survives_a_raising_stamper(self):
        """Fail-open is the contract: a stamping error costs the stamp, not the task."""
        with tempfile.TemporaryDirectory(prefix="slack-stamp-open-") as raw:
            ws = Path(raw)
            bridge = _load_slack(ws)
            import task_envelope
            orig = task_envelope.stamp_text
            try:
                task_envelope.stamp_text = lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("keychain on fire"))
                tid = f"task-{int(time.time() * 1000)}"
                tf = ws / "tasks" / f"{tid}.txt"
                tf.parent.mkdir(parents=True, exist_ok=True)
                bridge._write_routed_task(tf, "task: hello\n", tid,
                                          {"channel": "D1", "thread_ts": None})
                body = tf.read_text()
                self.assertIn("task: hello", body)
                self.assertNotIn("envelope_hmac:", body, "no partial stamp left behind")
                self.assertIn(tid, bridge.pending_replies,
                              "the route is still registered when stamping fails")
            finally:
                task_envelope.stamp_text = orig


class TelegramEdgeStampWiring(unittest.TestCase):
    """Source-level WIRING pin only — semantics are covered by the slack cases."""

    def test_stamps_the_content_it_then_writes(self):
        src = (REPO / "src" / "telegram-bridge.py").read_text()
        self.assertIn("from task_envelope import stamp_text", src)
        self.assertIn("_task_content = stamp_text(_task_content, REPO)", src,
                      "the stamp must be applied to the variable that is written")
        self.assertIn("task_file.write_text(_task_content)", src,
                      "the write must consume the stamped variable, not a fresh f-string")
        stamp_at = src.index("_task_content = stamp_text(")
        write_at = src.index("task_file.write_text(_task_content)")
        self.assertLess(stamp_at, write_at, "stamping must precede the write")


if __name__ == "__main__":
    unittest.main(verbosity=2)
