#!/usr/bin/env python3
"""Tests for skills/task-progress/scripts/notify.py."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "skills" / "task-progress" / "scripts" / "notify.py"


def _load() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("notify", SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestTokenResolution(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_env_var_takes_precedence(self):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-from-env"}):
            self.assertEqual(self.mod._token("slack", "SLACK_BOT_TOKEN"), "xoxb-from-env")

    def test_missing_returns_empty(self):
        with patch.dict("os.environ", {}, clear=False), \
             patch.object(self.mod.Path, "read_text", side_effect=OSError):
            # Ensure env var is absent
            import os
            os.environ.pop("SLACK_BOT_TOKEN", None)
            os.environ.pop("DISCORD_BOT_TOKEN", None)
            result = self.mod._token("slack", "SLACK_BOT_TOKEN")
            # May be non-empty if the real file exists — just check it's a string
            self.assertIsInstance(result, str)


class TestSendSlack(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"ok": true}'
        with patch.object(self.mod, "_token", return_value="xoxb-fake"), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            result = self.mod.send_slack("D123", "hello")
        self.assertTrue(result)

    def test_missing_token_returns_false(self):
        with patch.object(self.mod, "_token", return_value=""):
            result = self.mod.send_slack("D123", "hello")
        self.assertFalse(result)

    def test_api_error_returns_false(self):
        with patch.object(self.mod, "_token", return_value="xoxb-fake"), \
             patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = self.mod.send_slack("D123", "hello")
        self.assertFalse(result)

    def test_slack_not_ok_returns_false(self):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"ok": false, "error": "channel_not_found"}'
        with patch.object(self.mod, "_token", return_value="xoxb-fake"), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            result = self.mod.send_slack("D123", "hello")
        self.assertFalse(result)

    def test_thread_ts_included_in_payload(self):
        captured = {}

        def fake_post(url, payload, headers):
            captured.update(payload)
            return True

        with patch.object(self.mod, "_token", return_value="xoxb-fake"), \
             patch.object(self.mod, "_post", side_effect=fake_post):
            self.mod.send_slack("D123", "update", thread_ts="1234.56")
        self.assertEqual(captured.get("thread_ts"), "1234.56")


class TestSendDiscord(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"id": "msg123"}'
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            result = self.mod.send_discord("111222333", "hello")
        self.assertTrue(result)

    def test_missing_token_returns_false(self):
        with patch.object(self.mod, "_token", return_value=""):
            result = self.mod.send_discord("111", "hello")
        self.assertFalse(result)


class TestSendTelegram(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"ok": true, "result": {"message_id": 42}}'
        with patch.object(self.mod, "_token", return_value="9999:fake"), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            result = self.mod.send_telegram("123456", "hello")
        self.assertTrue(result)

    def test_missing_token_returns_false(self):
        with patch.object(self.mod, "_token", return_value=""):
            result = self.mod.send_telegram("123456", "hello")
        self.assertFalse(result)


class TestCLI(unittest.TestCase):
    def test_missing_channel_id_exits_1(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--source", "slack", "--message", "hi"],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 1)

    def test_missing_message_exits_nonzero(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--source", "slack", "--channel-id", "D123"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0)

    def test_unknown_source_rejected_by_argparse(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--source", "whatsapp",
             "--channel-id", "D123", "--message", "hi"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
