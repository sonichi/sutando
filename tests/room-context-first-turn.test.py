#!/usr/bin/env python3
"""Regression through the real canonical writer, parser and model prompt renderer."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()
REPO = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="context-first-turn-")
WS = Path(TEMP.name)
os.environ.update(SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=str(WS), REMOTE_TASK_TOKEN="test-only",
                  REMOTE_TASK_URL="http://127.0.0.1:1", REMOTE_TASK_TIER="owner", AGENT_MXID="@agent:example.test")
(WS / ".notes-migrated").touch()
(WS / ".build_log-migrated").touch()
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages/ag2-sparrow"))
from ag2_sparrow import task_io
from ag2_sparrow.adapters.codex import codex_connector


class FirstTurnTest(unittest.TestCase):
    def test_first_task_delivers_context_outside_the_actual_ask(self):
        spec = importlib.util.spec_from_file_location("first_turn_gateway", REPO / "src/remote-gateway-bridge.py")
        gateway = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = gateway
        spec.loader.exec_module(gateway)
        now = int(time.time())
        binding = dict(schema_version=1, context_id="ctx-first", agent_id="@agent:example.test", room_id="!first-room",
                       task_id="task-first", anchor_event_id="$ask", access_tier="owner", policy_revision="policy-1",
                       membership_epoch="membership-1", visibility="mentions", observed_at=now, expires_at=now + 60)
        context = dict(binding, status="partial", coverage=dict(completeness="unknown", truncated=False),
                       readme=dict(text="FIRST_ROOM_PURPOSE", content_hash="hash-1", truncated=False),
                       messages=[dict(event_id="$prior", sender="@friend:example.test", body="ELIGIBLE_PRIOR_MESSAGE", selection="mention")])
        with mock.patch.object(gateway, "_write_owner_activity"), mock.patch.object(gateway, "_req", side_effect=AssertionError("no client fetch")):
            tid, durable = gateway._write_task(dict(id="task-first", source="ag2space", channel_id="!first-room",
                                                   source_message_id="$ask", access_tier="owner", task="ACTUAL_USER_QUESTION",
                                                   room_context_binding=binding, room_context=context))
        self.assertTrue(durable)
        text = (gateway.TASKS_DIR / (tid + ".txt")).read_text()
        headers, ask, rules = task_io.parse_task(str(gateway.TASKS_DIR / (tid + ".txt")))
        self.assertEqual(ask, "ACTUAL_USER_QUESTION")
        self.assertIn("room_context", headers, "canonical writer dropped supplied first-turn context")
        self.assertEqual(json.loads(headers["room_context"])["readme"]["text"], "FIRST_ROOM_PURPOSE")
        prompt = codex_connector.wrap_untrusted(tid, headers, ask, rules)
        self.assertIn("FIRST_ROOM_PURPOSE", prompt)
        self.assertIn("ELIGIBLE_PRIOR_MESSAGE", prompt)
        self.assertNotIn("room_ops.py read", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
