#!/usr/bin/env python3
"""CONTEXT-FIRST must reach an owner task with NO optional skill installed.
Separate file: the sibling suites seed a notify.py stub, so neither can gate this."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
_tmp = tempfile.mkdtemp(prefix="slack-cf-")

os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
os.environ["SLACK_APP_TOKEN"] = "xapp-test-not-real"
# Deliberately EMPTY: no task-progress, no audio-transcribe.
os.environ["CLAUDE_CONFIG_DIR"] = str(Path(_tmp) / "claude")
Path(os.environ["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
# Seed before exec_module: config resolves at import and falls back to the
# operator's real access.json, so CLAUDE_CONFIG_DIR alone is not isolation.
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}')

sys.path.insert(0, str(REPO / "src"))
for name in ("slack_bolt", "slack_bolt.adapter", "slack_bolt.adapter.socket_mode"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["slack_bolt"].App = lambda *a, **k: types.SimpleNamespace(
    event=lambda *a, **k: (lambda f: f), client=types.SimpleNamespace())
sys.modules["slack_bolt.adapter.socket_mode"].SocketModeHandler = lambda *a, **k: None

_spec = importlib.util.spec_from_file_location("slack_bridge", REPO / "src" / "slack-bridge.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

# Isolated dirs: the checkout's tasks/ accumulates admitted ids across runs
# and already_admitted would replay-drop later test writes.
mod.TASKS_DIR = Path(_tmp) / "tasks"
mod.RESULTS_DIR = Path(_tmp) / "results"
mod.TASKS_DIR.mkdir(parents=True, exist_ok=True)
mod.RESULTS_DIR.mkdir(parents=True, exist_ok=True)


class ContextFirstUngated(unittest.TestCase):
    _seq = 0

    def _write(self, tier: str = "owner") -> str:
        uid = "U_OWNER"
        # Unique ts per call: the ingress replay-dedup (already_admitted) drops a
        # second admission of the same provider ts, so a reused ts reads test 1's file.
        ContextFirstUngated._seq += 1
        event = {"user": uid, "channel": "CFAKE", "channel_type": "im",
                 "ts": f"1000.{ContextFirstUngated._seq:03d}"}
        with patch.object(mod, "load_allowed", lambda: {uid}), \
             patch.object(mod, "_ensure_tier_map_seeded", lambda: True), \
             patch.object(mod, "load_tier_map", lambda: {uid: tier}), \
             patch.object(mod, "write_owner_activity", lambda *a, **k: None):
            mod._write_task(event, "DM", "please check the Zacks", "testowner")
        files = sorted(Path(mod.TASKS_DIR).glob("task-*.txt"))
        self.assertTrue(files, "no task file written")
        return files[-1].read_text()

    def test_neither_skill_installed_still_gets_context_first(self):
        notify = mod.claude_home_path("skills", "task-progress", "scripts", "notify.py")
        transcribe = mod.claude_home_path("skills", "audio-transcribe", "scripts", "transcribe.py")
        self.assertFalse(notify.exists(), "harness must have NO task-progress skill")
        self.assertFalse(transcribe.exists(), "harness must have NO audio-transcribe skill")

        body = self._write("owner")
        self.assertIn("===SKILL INSTRUCTIONS", body,
                      "owner task lost its instructions block because no optional skill was installed")
        self.assertIn("CONTEXT-FIRST", body,
                      "CONTEXT-FIRST is a correctness step and must not be gated on optional skills")

    def test_non_owner_still_gets_nothing(self):
        """The owner-only gate must survive the ungating — this is the half that
        should stay conditional."""
        body = self._write("other")
        self.assertNotIn("===SKILL INSTRUCTIONS", body)
        self.assertNotIn("CONTEXT-FIRST", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
