#!/usr/bin/env python3
"""Tests Telegram result routing for the standalone [dm-only] marker."""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_WORKSPACE_TMP = tempfile.mkdtemp(prefix="sutando-telegram-dm-only-test-")
os.environ["SUTANDO_WORKSPACE"] = _WORKSPACE_TMP
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")
_CONFIG_TMP = tempfile.mkdtemp(prefix="sutando-telegram-dm-only-config-")
os.environ["CLAUDE_CONFIG_DIR"] = _CONFIG_TMP
_TELEGRAM_CONFIG = Path(_CONFIG_TMP) / "channels" / "telegram"
_TELEGRAM_CONFIG.mkdir(parents=True, exist_ok=True)
(_TELEGRAM_CONFIG / "access.json").write_text('{"allowFrom": []}', encoding="utf-8")

spec = importlib.util.spec_from_file_location(
    "tbridge_dm_only", REPO / "src" / "telegram-bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class TestTelegramDmOnlyReply(unittest.TestCase):
    def test_group_route_uses_owner_from_access_data(self):
        parsed = bridge.parse_markers("[dm-only]\nPrivate reply")
        self.assertEqual(
            bridge._reply_destination(
                -100123, parsed, {"allowFrom": ["42"]}, None),
            42,
        )

    def test_group_route_uses_environment_override(self):
        parsed = bridge.parse_markers("[dm-only]\nPrivate reply")
        self.assertEqual(
            bridge._reply_destination(
                -100123, parsed, {"allowFrom": ["42"]}, "84"),
            84,
        )

    def test_private_route_is_unchanged(self):
        parsed = bridge.parse_markers("[dm-only]\nPrivate reply")
        self.assertEqual(
            bridge._reply_destination(42, parsed, {"allowFrom": ["84"]}, "84"),
            42,
        )

    def test_group_route_without_marker_is_unchanged(self):
        parsed = bridge.parse_markers("Ordinary reply")
        self.assertEqual(
            bridge._reply_destination(-100123, parsed, {"allowFrom": ["42"]}, None),
            -100123,
        )

    def test_group_route_without_owner_is_unroutable(self):
        parsed = bridge.parse_markers("[dm-only]\nPrivate reply")
        self.assertIsNone(
            bridge._reply_destination(-100123, parsed, {"allowFrom": []}, None))

    def test_non_integer_chat_id_is_not_treated_as_private(self):
        parsed = bridge.parse_markers("[dm-only]\nPrivate reply")
        self.assertEqual(
            bridge._reply_destination("malformed-chat-id", parsed, {"allowFrom": ["42"]}, None),
            42,
        )

    def test_non_integer_owner_id_is_unroutable(self):
        parsed = bridge.parse_markers("[dm-only]\nPrivate reply")
        self.assertIsNone(
            bridge._reply_destination(-100123, parsed, {"allowFrom": ["not-an-id"]}, None))

    def test_redirect_notice_retries_until_telegram_accepts(self):
        calls = []
        outcomes = [{"ok": False}, OSError("connection reset"), {"ok": True}]
        original_api = bridge.api
        original_file = bridge._PENDING_NOTICES_FILE
        state_file = Path(tempfile.mkdtemp()) / "pending-notices.json"
        bridge._PENDING_NOTICES_FILE = state_file
        def fake_api(method, **params):
            calls.append((method, params))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        bridge.api = fake_api
        saves = []
        original_save = bridge._save_pending_notices
        def counting_save(pending):
            saves.append(dict(pending))
            return original_save(pending)
        bridge._save_pending_notices = counting_save
        pending_notices = {}
        try:
            self.assertFalse(
                bridge._send_redirect_notice(pending_notices, "task-9", "-100999"))
            self.assertEqual(pending_notices, {"task-9": "-100999"})
            # A refused notice is persisted so a bridge restart keeps retrying.
            self.assertEqual(
                json.loads(state_file.read_text()), {"task-9": "-100999"})
            # A raising api() also counts as a failed notice, not a crash.
            self.assertFalse(
                bridge._send_redirect_notice(pending_notices, "task-9", "-100999"))
            self.assertEqual(pending_notices, {"task-9": "-100999"})
            # Refusing again with the entry unchanged does not rewrite the file.
            self.assertEqual(len(saves), 1)
            self.assertTrue(
                bridge._send_redirect_notice(pending_notices, "task-9", "-100999"))
            self.assertEqual(pending_notices, {})
            self.assertEqual(json.loads(state_file.read_text()), {})
        finally:
            bridge.api = original_api
            bridge._save_pending_notices = original_save
            bridge._PENDING_NOTICES_FILE = original_file
        self.assertEqual(
            calls,
            [("sendMessage", {"chat_id": "-100999",
                              "text": "Sent you the reply in a private chat."})] * 3)

    def test_pending_notices_write_failure_is_swallowed(self):
        # A state dir that cannot be created must not crash the queue — the
        # entry stays in memory for a later pass to retry.
        original_file = bridge._PENDING_NOTICES_FILE
        blocker = Path(tempfile.mkdtemp()) / "not-a-dir"
        blocker.write_text("x")
        bridge._PENDING_NOTICES_FILE = blocker / "pending-notices.json"
        try:
            bridge._save_pending_notices({"task-1": "-1"})
        finally:
            bridge._PENDING_NOTICES_FILE = original_file

    def test_pending_notices_corrupt_file_loads_empty(self):
        # A corrupt persisted queue starts empty rather than crashing the loop.
        original_file = bridge._PENDING_NOTICES_FILE
        bad = Path(tempfile.mkdtemp()) / "pending-notices.json"
        bad.write_text("{not json")
        bridge._PENDING_NOTICES_FILE = bad
        try:
            self.assertEqual(bridge._load_pending_notices(), {})
        finally:
            bridge._PENDING_NOTICES_FILE = original_file


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(TestTelegramDmOnlyReply))
    raise SystemExit(0 if result.wasSuccessful() else 1)
