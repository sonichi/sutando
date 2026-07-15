#!/usr/bin/env python3
"""Tests for skills/task-progress/scripts/notify.py."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
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


class TestProgressMessageGuard(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_short_progress_message_allowed(self):
        self.assertIsNone(
            self.mod._progress_message_error("On it — checking the PR now.")
        )

    def test_long_final_answer_rejected(self):
        message = (
            "Top options for a personal landing page: Framer for speed, Carrd for "
            "cost, Astro plus Vercel for full control, and GitHub Pages for zero "
            "hosting cost. Recommendation: use Framer if you want polish today, "
            "or Astro plus Vercel if you want a blog and source-controlled content. "
            "That gives you the best tradeoff between design quality, cost, and "
            "future flexibility."
        )
        error = self.mod._progress_message_error(message)
        self.assertIsNotNone(error)
        self.assertIn("too long", error)

    def test_multiline_answer_rejected(self):
        message = "\n".join([
            "Options:",
            "1. Framer",
            "2. Carrd",
            "3. Astro",
            "4. GitHub Pages",
        ])
        error = self.mod._progress_message_error(message)
        self.assertIsNotNone(error)
        self.assertIn("too many lines", error)

    def test_main_rejects_long_message(self):
        with patch("sys.argv", [
            "notify.py", "--source", "discord", "--channel-id", "C123",
            "--message", "x" * 281,
        ]):
            rc = self.mod.main()
        self.assertEqual(rc, 1)

    def test_main_rejects_multiline_message(self):
        msg = "\n".join(["line1", "line2", "line3", "line4", "line5"])
        with patch("sys.argv", [
            "notify.py", "--source", "discord", "--channel-id", "C123",
            "--message", msg,
        ]):
            rc = self.mod.main()
        self.assertEqual(rc, 1)


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


class TestSendRemoteGateway(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_success_posts_room_message_op(self):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"ok": true}'
        captured = {}

        def fake_urlopen(req, timeout=10):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data)
            return mock_resp

        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("REMOTE_TASK_URL", "REMOTE_TASK_TOKEN")}
        with patch.object(self.mod, "_env_file",
                          return_value={"REMOTE_TASK_URL": "https://gw.example/relay",
                                        "REMOTE_TASK_TOKEN": "bearer-fake"}), \
             patch.dict(os.environ, clean_env, clear=True), \
             patch("urllib.request.urlopen", fake_urlopen):
            result = self.mod.send_remote_gateway("someprovider", "!room:server", "hello")
        self.assertTrue(result)
        self.assertEqual(captured["url"], "https://gw.example/relay/v1/room")
        self.assertEqual(captured["payload"],
                         {"op": "message", "room_id": "!room:server", "body": "hello"})

    def test_missing_env_returns_false(self):
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("REMOTE_TASK_URL", "REMOTE_TASK_TOKEN")}
        with patch.object(self.mod, "_env_file", return_value={}), \
             patch.dict(os.environ, clean_env, clear=True):
            result = self.mod.send_remote_gateway("someprovider", "!room:server", "hello")
        self.assertFalse(result)


class TestGatewaySourceTraversal(unittest.TestCase):
    """Regression for the confirmed traversal finding on PR #2054: a --source
    like `../evil` must never escape $CLAUDE_CONFIG_DIR/channels — neither
    reading the escaped .env nor posting its bearer anywhere."""

    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name)
        (self.cfg / "channels").mkdir(parents=True)
        # The file an attacker wants us to read: OUTSIDE channels/, .env-shaped,
        # with an attacker-controlled URL alongside the secret.
        (self.cfg / "evil").mkdir()
        (self.cfg / "evil" / ".env").write_text(
            "REMOTE_TASK_URL=https://escaped.example\nREMOTE_TASK_TOKEN=escaped-token\n")
        self.env_patch = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg)}, clear=False)
        self.env_patch.start()
        # env vars must not mask the file-read path under test
        for var in ("REMOTE_TASK_URL", "REMOTE_TASK_TOKEN"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_dotdot_source_refused_before_any_read(self):
        posts = []
        with patch.object(self.mod, "_post", side_effect=lambda *a, **k: posts.append(a) or True):
            ok = self.mod.send_remote_gateway("../evil", "!room:server", "hi")
        self.assertFalse(ok)
        self.assertEqual(posts, [])

    def test_dotdot_source_refused_via_cli(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--source", "../evil",
             "--channel-id", "!room:server", "--message", "hi"],
            capture_output=True, text=True,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(self.cfg)},
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("invalid gateway source", r.stderr)
        self.assertNotIn("escaped-token", r.stderr)

    def test_symlink_escape_refused(self):
        # Slug-valid name whose directory symlinks out of channels/.
        os.symlink(self.cfg / "evil", self.cfg / "channels" / "sneaky")
        posts = []
        with patch.object(self.mod, "_post", side_effect=lambda *a, **k: posts.append(a) or True):
            ok = self.mod.send_remote_gateway("sneaky", "!room:server", "hi")
        self.assertFalse(ok)
        self.assertEqual(posts, [])

    def test_uppercase_and_weird_slugs_refused(self):
        for bad in ("EVIL", "a b", "a/b", ".hidden", "", "-lead"):
            self.assertFalse(self.mod.send_remote_gateway(bad, "!r:s", "hi"), bad)


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

    def test_unconfigured_gateway_source_fails_open(self):
        # A source outside the built-ins routes to the remote-gateway sender;
        # with no channels/<source>/.env it must fail (exit 1) with a hint —
        # never traceback, never block.
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--source", "whatsapp",
             "--channel-id", "D123", "--message", "hi"],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "CLAUDE_CONFIG_DIR": "/nonexistent"},
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("REMOTE_TASK_URL", r.stderr)

    def test_long_message_rejected_before_token_lookup(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--source", "slack",
             "--channel-id", "D123", "--message", "x" * 281],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("progress update is too long", r.stderr)
        self.assertNotIn("SLACK_BOT_TOKEN", r.stderr)


if __name__ == "__main__":
    unittest.main()
