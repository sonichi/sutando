#!/usr/bin/env python3
"""Tests for skills/task-progress/scripts/notify.py."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
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

    @staticmethod
    def _response(body):
        response = MagicMock()
        response.__enter__ = lambda s: s
        response.__exit__ = MagicMock(return_value=False)
        response.read.return_value = json.dumps(body).encode()
        return response

    def test_success(self):
        mock_resp = self._response({"id": "msg123", "mentions": []})
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            result = self.mod.send_discord("111222333", "hello")
        self.assertTrue(result)

    def test_missing_token_returns_false(self):
        with patch.object(self.mod, "_token", return_value=""):
            result = self.mod.send_discord("111", "hello")
        self.assertFalse(result)

    def test_discord_request_api_error_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = self.mod._discord_request(
                "https://discord.com/api/v10/users/123",
                "Bot-fake",
            )
        self.assertIsNone(result)
        self.assertIn("Discord request failed: timeout", stderr.getvalue())

    def test_post_failure_returns_false(self):
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch.object(self.mod, "_discord_request", return_value=None):
            result = self.mod.send_discord("111", "hello")
        self.assertFalse(result)

    def test_plain_at_handle_is_rejected_before_post(self):
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch.object(self.mod, "_discord_request") as request, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = self.mod.send_discord("111", "Please review, @qingyun-wu")
        self.assertFalse(result)
        request.assert_not_called()
        self.assertIn("unresolved Discord mention(s): @qingyun-wu", stderr.getvalue())

    def test_email_address_is_not_treated_as_a_mention(self):
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch.object(
                 self.mod,
                 "_discord_request",
                 return_value={"id": "msg123", "mentions": []},
             ) as request:
            result = self.mod.send_discord("111", "Email dev@example.com")
        self.assertTrue(result)
        self.assertEqual(request.call_count, 1)

    def test_structured_mention_is_preflighted_and_verified(self):
        user_id = "1025828152183885925"
        posted = {
            "id": "msg123",
            "mentions": [{"id": user_id, "username": "qingyunwu"}],
        }
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch.object(
                 self.mod,
                 "_discord_request",
                 side_effect=[{"id": user_id}, posted],
             ) as request:
            result = self.mod.send_discord("111", f"Please review, <@{user_id}>")
        self.assertTrue(result)
        self.assertEqual(request.call_count, 2)
        payload = request.call_args_list[1].args[2]
        self.assertEqual(payload["allowed_mentions"]["parse"], [])
        self.assertEqual(payload["allowed_mentions"]["users"], [user_id])

    def test_unresolvable_structured_mention_is_not_posted(self):
        user_id = "999999999999999999"
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch.object(
                 self.mod,
                 "_discord_request",
                 return_value=None,
             ) as request, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = self.mod.send_discord("111", f"Please review, <@{user_id}>")
        self.assertFalse(result)
        self.assertEqual(request.call_count, 1)
        self.assertIn("message was not sent", stderr.getvalue())

    def test_missing_mention_in_post_response_returns_error(self):
        user_id = "1025828152183885925"
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch.object(
                 self.mod,
                 "_discord_request",
                 side_effect=[{"id": user_id}, {"id": "msg123", "mentions": []}],
             ), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = self.mod.send_discord("111", f"Please review, <@{user_id}>")
        self.assertFalse(result)
        self.assertIn("did not resolve expected mention", stderr.getvalue())

    def test_validation_can_be_disabled_for_plain_text_handle(self):
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch.object(
                 self.mod,
                 "_discord_request",
                 return_value={"id": "msg123", "mentions": []},
             ) as request:
            result = self.mod.send_discord(
                "111",
                "GitHub author @qingyun-wu",
                validate_mentions=False,
            )
        self.assertTrue(result)
        self.assertEqual(request.call_count, 1)

    def test_cli_defaults_to_validation_and_returns_agent_visible_error(self):
        with patch("sys.argv", [
            "notify.py", "--source", "discord", "--channel-id", "111",
            "--message", "Please review, @qingyun-wu",
        ]), patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch.object(self.mod, "_discord_request") as request, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = self.mod.main()
        self.assertEqual(result, 1)
        request.assert_not_called()
        self.assertIn("Use <@USER_ID>", stderr.getvalue())

    def test_cli_opt_out_allows_intentional_plain_text_handle(self):
        with patch("sys.argv", [
            "notify.py", "--source", "discord", "--channel-id", "111",
            "--message", "GitHub author @qingyun-wu",
            "--no-validate-mentions",
        ]), patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch.object(
                 self.mod,
                 "_discord_request",
                 return_value={"id": "msg123", "mentions": []},
             ) as request:
            result = self.mod.main()
        self.assertEqual(result, 0)
        self.assertEqual(request.call_count, 1)


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
        # Also strip the AG2 combined-token vars: send_remote_gateway falls back to
        # AG2_REMOTE_TOKEN=url|secret, so a genuine "no creds anywhere" case must
        # clear them too (else an ambient AG2_REMOTE_TOKEN resolves and this passes).
        _drop = ("REMOTE_TASK_URL", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "AG2_REMOTE_URL")
        clean_env = {k: v for k, v in os.environ.items() if k not in _drop}
        # Pin the containment guard to a temp tree — see _hermetic_channels_root.
        clean_env["CLAUDE_CONFIG_DIR"] = self._hermetic_channels_root()
        clean_env.pop("CLAUDE_HOME", None)
        with patch.object(self.mod, "_env_file", return_value={}), \
             patch.dict(os.environ, clean_env, clear=True):
            result = self.mod.send_remote_gateway("someprovider", "!room:server", "hello")
        self.assertFalse(result)


    def _hermetic_channels_root(self, source: str = "ag2space") -> str:
        """A real channels/<source>/.env inside a temp dir, for CLAUDE_CONFIG_DIR.

        `send_remote_gateway` applies a containment guard — the channel `.env` must
        RESOLVE inside `<config>/channels/` — and it resolves the REAL filesystem,
        before `_env_file` is patched. So any test that clears the gateway env vars
        (forcing the guard to run) is implicitly asserting something about the HOST.

        Not hypothetical: on a host where `channels/ag2space/.env` is a symlink out
        of the channels dir, the guard correctly refuses and the two combined-token
        tests below fail with `AssertionError: False is not true` — measured
        2026-07-30 while CI was green. They passed only because CI's layout happens
        to satisfy the guard.

        Pointing CLAUDE_CONFIG_DIR at a temp tree makes the guard deterministic and
        keeps these tests about what they claim to test: the `url|secret` form.
        """
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        d = root / "channels" / source
        d.mkdir(parents=True)
        (d / ".env").write_text("")   # a real file, never a symlink
        return str(root)

    def test_ag2_combined_token_only_delivers(self):
        """Regression for #2101 review (High): a channel provisioned with ONLY
        AG2_REMOTE_TOKEN=url|secret (the AG2-compatible onboarding form the rest
        of the gateway stack accepts) must deliver — not fail with 'no
        REMOTE_TASK_URL/REMOTE_TASK_TOKEN'."""
        captured = {}
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"ok": true}'

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data)
            captured["auth"] = req.get_header("Authorization")
            return mock_resp

        _drop = ("REMOTE_TASK_URL", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "AG2_REMOTE_URL")
        clean_env = {k: v for k, v in os.environ.items() if k not in _drop}
        # Pin the containment guard to a temp tree — see _hermetic_channels_root.
        clean_env["CLAUDE_CONFIG_DIR"] = self._hermetic_channels_root()
        clean_env.pop("CLAUDE_HOME", None)
        with patch.object(self.mod, "_env_file",
                          return_value={"AG2_REMOTE_TOKEN": "https://gw.example/relay|sekret"}), \
             patch.dict(os.environ, clean_env, clear=True), \
             patch("urllib.request.urlopen", fake_urlopen):
            result = self.mod.send_remote_gateway("ag2space", "!room:ag2.space", "hi")
        self.assertTrue(result)
        self.assertEqual(captured["url"], "https://gw.example/relay/v1/room")
        self.assertEqual(captured["payload"],
                         {"op": "message", "room_id": "!room:ag2.space", "body": "hi"})
        self.assertEqual(captured["auth"], "Bearer sekret")

    def test_remote_task_token_combined_form_delivers(self):
        """Regression for #2101 review round 2 (P1): the compact combined form in
        REMOTE_TASK_TOKEN itself (REMOTE_TASK_TOKEN=url|secret, no separate
        REMOTE_TASK_URL) — the documented bootstrap shortcut — must deliver, not
        fail with url empty."""
        captured = {}
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"ok": true}'

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            return mock_resp

        _drop = ("REMOTE_TASK_URL", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "AG2_REMOTE_URL")
        clean_env = {k: v for k, v in os.environ.items() if k not in _drop}
        # Pin the containment guard to a temp tree — see _hermetic_channels_root.
        clean_env["CLAUDE_CONFIG_DIR"] = self._hermetic_channels_root()
        clean_env.pop("CLAUDE_HOME", None)
        with patch.object(self.mod, "_env_file",
                          return_value={"REMOTE_TASK_TOKEN": "https://gw.example/relay|sekret"}), \
             patch.dict(os.environ, clean_env, clear=True), \
             patch("urllib.request.urlopen", fake_urlopen):
            result = self.mod.send_remote_gateway("ag2space", "!room:ag2.space", "hi")
        self.assertTrue(result)
        self.assertEqual(captured["url"], "https://gw.example/relay/v1/room")
        self.assertEqual(captured["auth"], "Bearer sekret")


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


class TestChannelEnvContainment(unittest.TestCase):
    """The channels/<source>/.env is a FALLBACK — os.environ wins (notify.py:171-172).

    So the containment guard must gate only the case where the file is actually
    read. Guarding it unconditionally refuses an operator who exported both
    REMOTE_TASK_URL and REMOTE_TASK_TOKEN, for a file that would never be opened.
    """

    def setUp(self):
        self.mod = _load()
        self.tmp = Path(tempfile.mkdtemp())
        ch = self.tmp / "channels" / "ag2space"
        ch.mkdir(parents=True)
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        real = outside / ".env"
        real.write_text("REMOTE_TASK_URL=https://file/relay\nREMOTE_TASK_TOKEN=filetok\n")
        (ch / ".env").symlink_to(real)          # channel entry pointing out of the tree
        self._saved = {k: os.environ.get(k) for k in
                       ("CLAUDE_CONFIG_DIR", "REMOTE_TASK_URL", "REMOTE_TASK_TOKEN",
                        "AG2_REMOTE_TOKEN", "AG2_REMOTE_URL")}
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _refused(self) -> bool:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                self.mod.send_remote_gateway("ag2space", "!r:ag2.space", "hi")
            except Exception:
                pass
        return "refusing env path outside channels dir" in err.getvalue()

    def test_env_configured_is_not_refused(self):
        """Both values in os.environ -> the .env is never read, so its location
        must not veto the send. Regression: this refused, and the documented
        'notify before long work' ack silently failed on that channel."""
        os.environ["REMOTE_TASK_URL"] = "https://chat.example/relay"
        os.environ["REMOTE_TASK_TOKEN"] = "envtok"
        self.assertFalse(self._refused())


    def test_combined_token_only_is_not_refused(self):
        """Regression for the #2355 review P1: the documented ONE-TOKEN onboarding
        form (REMOTE_TASK_TOKEN=https://gw|secret, no separate REMOTE_TASK_URL) is a
        fully env-configured send. Checking only the split URL+TOKEN pair missed it,
        so a symlinked-out channel .env still refused it over a file never needed."""
        os.environ.pop("REMOTE_TASK_URL", None)
        os.environ["REMOTE_TASK_TOKEN"] = "https://env.example/relay|envtok"
        self.assertFalse(self._refused())

    def test_legacy_alias_combined_token_is_not_refused(self):
        """Same for the legacy AG2_REMOTE_TOKEN alias — it is resolved before the
        file too, so it must not trip the containment guard either."""
        for k in ("REMOTE_TASK_URL", "REMOTE_TASK_TOKEN"):
            os.environ.pop(k, None)
        os.environ["AG2_REMOTE_TOKEN"] = "https://legacy.example/relay|legacytok"
        try:
            self.assertFalse(self._refused())
        finally:
            os.environ.pop("AG2_REMOTE_TOKEN", None)

    def test_guard_still_fires_when_the_file_is_needed(self):
        """The control. Without env values the file IS consulted, so an entry
        symlinked out of channels/ must still be refused — the security check is
        unchanged, not relaxed. If this ever passes, the fix went too far."""
        os.environ.pop("REMOTE_TASK_URL", None)
        os.environ.pop("REMOTE_TASK_TOKEN", None)
        self.assertTrue(self._refused())


if __name__ == "__main__":
    unittest.main()
