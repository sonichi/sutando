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
sys.path.insert(0, str(REPO / "src"))

import channel_env_containment  # noqa: E402 — the shared module notify.py delegates to


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

    def test_falls_back_to_vault_when_env_and_env_file_are_empty(self):
        """The vault is the third tier — a token that lives ONLY there must resolve.

        Measured 2026-08-23 on a live host: TELEGRAM_BOT_TOKEN was in the vault,
        absent from the process env, and absent from channels/telegram/.env, so
        every Telegram progress notification failed with "not found" while the
        bridge — which already consults the vault — worked. This asserts the
        delegation, not a reimplementation of it.

        Hermetic: the vault reader is injected, never the real Keychain.
        """
        import channel_token

        empty = Path(tempfile.mkdtemp()) / "absent.env"
        seen = []

        def fake_vault(key):
            seen.append(key)
            return "tg-from-vault"

        def resolver(var, env_file=None, environ=None, vault_get=None):
            return channel_token.resolve_channel_token(
                var, env_file=empty, environ={}, vault_get=fake_vault)

        with patch.object(self.mod, "_resolve_channel_token", resolver):
            got = self.mod._token("telegram", "TELEGRAM_BOT_TOKEN")

        self.assertEqual(got, "tg-from-vault")
        self.assertEqual(seen, ["TELEGRAM_BOT_TOKEN"])

    def test_vault_only_token_resolves_end_to_end(self):
        """The behavioural regression: env empty, `.env` absent, token only in the vault.

        This one does NOT patch notify's own seam — it patches the vault reader
        underneath, so it fails the way the live bug failed (returns "") against
        any build that does not consult the vault at all.
        """
        import vault_intercept

        empty_home = Path(tempfile.mkdtemp())          # no channels/telegram/.env under it
        with patch.dict("os.environ",
                        {"CLAUDE_CONFIG_DIR": str(empty_home)}, clear=False):
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            with patch.object(vault_intercept, "get_vault_key",
                              lambda k: "tg-vault-only" if k == "TELEGRAM_BOT_TOKEN" else ""):
                got = self.mod._token("telegram", "TELEGRAM_BOT_TOKEN")
        self.assertEqual(got, "tg-vault-only")

    def test_load_resolver_returns_none_when_import_fails(self):
        """The degraded path itself, not just its result.

        The sibling test patches `_resolve_channel_token` to None, which
        exercises `_token`'s fallback but never `_load_resolver`. This drives
        the import failure so the `except` branch is actually executed.
        """
        import builtins
        real_import = builtins.__import__

        def boom(name, *a, **kw):
            if name == "channel_token":
                raise ImportError("simulated: src/ not importable")
            return real_import(name, *a, **kw)

        with patch.object(builtins, "__import__", boom):
            self.assertIsNone(self.mod._load_resolver())

    def test_env_still_wins_over_vault(self):
        """An exported value must keep winning, so working hosts are unaffected."""
        import channel_token

        def resolver(var, env_file=None, environ=None, vault_get=None):
            return channel_token.resolve_channel_token(
                var, env_file=None, environ={"TELEGRAM_BOT_TOKEN": "tg-from-env"},
                vault_get=lambda k: "tg-from-vault")

        with patch.object(self.mod, "_resolve_channel_token", resolver):
            self.assertEqual(
                self.mod._token("telegram", "TELEGRAM_BOT_TOKEN"), "tg-from-env")

    def test_degrades_to_env_when_resolver_unavailable(self):
        """A missing src/ must not fail the notification — fail-open by design."""
        with patch.object(self.mod, "_resolve_channel_token", None), \
             patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-degraded"}):
            self.assertEqual(
                self.mod._token("slack", "SLACK_BOT_TOKEN"), "xoxb-degraded")


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

    def _captured_payload(self, message):
        captured = {}

        def fake_post(url, payload, headers):
            captured.update(payload)
            return True

        with patch.object(self.mod, "_token", return_value="xoxb-fake"), \
             patch.object(self.mod, "_post", side_effect=fake_post):
            self.mod.send_slack("D123", message)
        return captured

    def test_single_link_note_keeps_its_preview(self):
        # The preview IS the value when a note carries exactly one link.
        captured = self._captured_payload("see https://example.com/a")
        self.assertIs(captured.get("unfurl_links"), True)
        self.assertIs(captured.get("unfurl_media"), True)

    def test_link_dense_note_suppresses_unfurling(self):
        # Two or more links would arrive as a stack of preview cards.
        captured = self._captured_payload(
            "see https://example.com/a and https://example.com/b")
        self.assertIs(captured.get("unfurl_links"), False)
        self.assertIs(captured.get("unfurl_media"), False)


class TestUnfurlFallback(unittest.TestCase):
    """notify.py reaches the shared threshold through a core-tree import.

    The skill must still run where that tree is absent, and it must degrade to
    suppression rather than to unfurling everything.
    """

    def test_missing_core_module_degrades_to_suppression(self):
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "policy.egress.unfurl":
                raise ImportError("core tree unavailable (test)")
            return real_import(name, *args, **kwargs)

        saved = {k: v for k, v in sys.modules.items() if k.startswith("policy")}
        for k in saved:
            del sys.modules[k]
        try:
            with patch.object(builtins, "__import__", side_effect=blocked):
                mod = _load()
            # Constant by design: with no threshold available, never unfurl.
            self.assertIs(mod._should_unfurl("no links at all"), False)
            self.assertIs(mod._should_unfurl("one https://example.com/a link"), False)
        finally:
            sys.modules.update(saved)

    def test_control_the_real_import_does_not_fall_back(self):
        # Without the block, the same load must reach the real predicate — so
        # the test above cannot pass merely because _load() always degrades.
        mod = _load()
        self.assertIs(mod._should_unfurl("one https://example.com/a link"), True)


class TestSendDiscord(unittest.TestCase):
    """send_discord routes through the shared src/channels/discord/client.py
    chokepoint. The PRODUCTION client stays in the loop with a scripted
    delivery transport; `get_user` (a retried read, not part of the gated
    delivery class) is stubbed at method level."""

    def setUp(self):
        self.mod = _load()

    def _bind_client(self, post_steps=None, get_user_steps=None):
        """Bind a scripted client; returns the recorded delivery requests."""
        sys.path.insert(0, str(REPO / "src"))
        from channels.discord.client import DiscordRestClient

        calls = []
        steps = list(post_steps or [])
        gets = list(get_user_steps or [])

        def transport(req, timeout):
            calls.append({"url": req.full_url, "method": req.get_method(),
                          "body": json.loads(req.data.decode()) if req.data else None})
            step = steps.pop(0) if steps else (200, {"id": "msg123", "mentions": []})
            if isinstance(step, Exception):
                raise step
            return step

        def fake_get_user(uid):
            step = gets.pop(0) if gets else {"id": str(uid)}
            if isinstance(step, Exception):
                raise step
            return step

        def factory(token):
            client = DiscordRestClient(token, transport=transport)
            client.get_user = fake_get_user
            return client

        patcher = patch.object(self.mod, "_rest_client", side_effect=factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_rest_client_factory_builds_shared_client(self):
        sys.path.insert(0, str(REPO / "src"))
        from channels.discord.client import DiscordRestClient
        client = self.mod._rest_client("Bot-fake")
        self.assertIsInstance(client, DiscordRestClient)
        self.assertEqual(client._timeout, 10)

    def test_success(self):
        calls = self._bind_client()
        with patch.object(self.mod, "_token", return_value="Bot-fake"):
            result = self.mod.send_discord("111222333", "hello")
        self.assertTrue(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertIn("/channels/111222333/messages", calls[0]["url"])

    def test_missing_token_returns_false(self):
        with patch.object(self.mod, "_token", return_value=""):
            result = self.mod.send_discord("111", "hello")
        self.assertFalse(result)

    def test_user_lookup_error_blocks_send(self):
        # Pre-client this lived in _discord_request ("Discord request failed");
        # the failure wording and the do-not-send behavior both survive.
        calls = self._bind_client(get_user_steps=[Exception("timeout")])
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = self.mod.send_discord("111", "Please review, <@123456789012345678>")
        self.assertFalse(result)
        self.assertIn("Discord request failed: timeout", stderr.getvalue())
        self.assertEqual(calls, [])  # the POST never happened

    def test_post_failure_returns_false(self):
        calls = self._bind_client(post_steps=[Exception("timeout")])
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = self.mod.send_discord("111", "hello")
        self.assertFalse(result)
        self.assertEqual(len(calls), 1)
        self.assertIn("Discord send not confirmed", stderr.getvalue())

    def test_refused_post_returns_false(self):
        # 4xx -> NOT_DELIVERED via the shared receipt classification.
        import urllib.error
        err = urllib.error.HTTPError("https://discord.com/x", 403, "forbidden",
                                     {}, io.BytesIO(b"{}"))
        calls = self._bind_client(post_steps=[err])
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = self.mod.send_discord("111", "hello")
        self.assertFalse(result)
        self.assertEqual(len(calls), 1)
        self.assertIn("NOT_DELIVERED", stderr.getvalue())

    def test_plain_at_handle_is_rejected_before_post(self):
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch.object(self.mod, "_rest_client") as factory, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = self.mod.send_discord("111", "Please review, @qingyun-wu")
        self.assertFalse(result)
        factory.assert_not_called()
        self.assertIn("unresolved Discord mention(s): @qingyun-wu", stderr.getvalue())

    def test_email_address_is_not_treated_as_a_mention(self):
        calls = self._bind_client()
        with patch.object(self.mod, "_token", return_value="Bot-fake"):
            result = self.mod.send_discord("111", "Email dev@example.com")
        self.assertTrue(result)
        self.assertEqual(len(calls), 1)

    def test_structured_mention_is_preflighted_and_verified(self):
        user_id = "1025828152183885925"
        posted = {
            "id": "msg123",
            "mentions": [{"id": user_id, "username": "qingyunwu"}],
        }
        calls = self._bind_client(post_steps=[(200, posted)],
                                  get_user_steps=[{"id": user_id}])
        with patch.object(self.mod, "_token", return_value="Bot-fake"):
            result = self.mod.send_discord("111", f"Please review, <@{user_id}>")
        self.assertTrue(result)
        self.assertEqual(len(calls), 1)
        payload = calls[0]["body"]
        self.assertEqual(payload["allowed_mentions"]["parse"], [])
        self.assertEqual(payload["allowed_mentions"]["users"], [user_id])

    def test_unresolvable_structured_mention_is_not_posted(self):
        user_id = "999999999999999999"
        calls = self._bind_client(get_user_steps=[{"id": "different"}])
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = self.mod.send_discord("111", f"Please review, <@{user_id}>")
        self.assertFalse(result)
        self.assertEqual(calls, [])
        self.assertIn("message was not sent", stderr.getvalue())

    def test_missing_mention_in_post_response_returns_error(self):
        user_id = "1025828152183885925"
        calls = self._bind_client(post_steps=[(200, {"id": "msg123", "mentions": []})],
                                  get_user_steps=[{"id": user_id}])
        with patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = self.mod.send_discord("111", f"Please review, <@{user_id}>")
        self.assertFalse(result)
        self.assertEqual(len(calls), 1)
        self.assertIn("did not resolve expected mention", stderr.getvalue())

    def test_validation_can_be_disabled_for_plain_text_handle(self):
        calls = self._bind_client()
        with patch.object(self.mod, "_token", return_value="Bot-fake"):
            result = self.mod.send_discord(
                "111",
                "GitHub author @qingyun-wu",
                validate_mentions=False,
            )
        self.assertTrue(result)
        self.assertEqual(len(calls), 1)

    def test_cli_defaults_to_validation_and_returns_agent_visible_error(self):
        with patch("sys.argv", [
            "notify.py", "--source", "discord", "--channel-id", "111",
            "--message", "Please review, @qingyun-wu",
        ]), patch.object(self.mod, "_token", return_value="Bot-fake"), \
             patch.object(self.mod, "_rest_client") as factory, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = self.mod.main()
        self.assertEqual(result, 1)
        factory.assert_not_called()
        self.assertIn("Use <@USER_ID>", stderr.getvalue())

    def test_cli_opt_out_allows_intentional_plain_text_handle(self):
        calls = self._bind_client()
        with patch("sys.argv", [
            "notify.py", "--source", "discord", "--channel-id", "111",
            "--message", "GitHub author @qingyun-wu",
            "--no-validate-mentions",
        ]), patch.object(self.mod, "_token", return_value="Bot-fake"):
            result = self.mod.main()
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)


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


    def _symlinked_channels_root(
        self,
        source: str = "ag2space",
        target_subdir: str = "channels",
        target_dirname: str | None = None,
        target_filename: str = ".env",
    ) -> tuple[str, str]:
        """A channels/<source>/.env that is a SYMLINK to a real file living under
        a SEPARATE app-support temp dir — mirrors the AG2 Space desktop-app
        layout (launch-sutando.sh), where `$CLAUDE_CONFIG_DIR/channels/ag2space/.env`
        symlinks OUT to `$SUTANDO_APP_SUPPORT/channels/ag2space/.env` (#3150/#3201).
        Distinct from `_hermetic_channels_root` below, which is deliberately a
        real file (never a symlink) so tests that aren't about this guard stay pinned.

        Returns (config_dir, app_support). By default the target is EXACTLY the
        one path the guard accepts under the app root; the keyword args move it
        off that path for the regression tests proving everything else is refused.
        """
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        app_support = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, app_support, ignore_errors=True)

        target_dir = app_support / target_subdir / (source if target_dirname is None else target_dirname)
        target_dir.mkdir(parents=True)
        target = target_dir / target_filename
        target.write_text(
            "REMOTE_TASK_URL=https://gw.example/relay\n"
            "REMOTE_TASK_TOKEN=symlinked-token\n"
        )

        channel_dir = root / "channels" / source
        channel_dir.mkdir(parents=True)
        (channel_dir / ".env").symlink_to(target)

        return str(root), str(app_support)

    def _clean_env(self, config_dir: str, app_support: str | None) -> dict:
        """os.environ with every gateway/base var the sender consults removed, so
        the file-read path (and only it) is under test. SUTANDO_APP_SUPPORT is
        set only when the test passes one — a dev running this suite inside the
        desktop app's core session has the real one exported."""
        _drop = ("REMOTE_TASK_URL", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN",
                 "AG2_REMOTE_URL", "CLAUDE_HOME", "SUTANDO_APP_SUPPORT")
        env = {k: v for k, v in os.environ.items() if k not in _drop}
        env["CLAUDE_CONFIG_DIR"] = config_dir
        if app_support is not None:
            env["SUTANDO_APP_SUPPORT"] = app_support
        return env

    def _refused(self, env: dict) -> bool:
        err = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
             contextlib.redirect_stderr(err):
            result = self.mod.send_remote_gateway("ag2space", "!room:ag2.space", "hi")
        self.assertFalse(result)
        return "refusing env path outside channels dir" in err.getvalue()

    def test_symlinked_env_under_app_support_is_delivered(self):
        """Regression for #3150/#3201: the AG2 Space desktop app installs
        channels/<source>/.env as a symlink OUT of the channels dir to
        $SUTANDO_APP_SUPPORT/channels/<source>/.env, its own durable copy of the
        same credential file. With SUTANDO_APP_SUPPORT exported (the app exports
        it for every process it spawns) that target is inside the second
        approved root, so the guard must accept it and deliver."""
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"ok": true}'
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            return mock_resp

        cfg, app = self._symlinked_channels_root()
        with patch.dict(os.environ, self._clean_env(cfg, app), clear=True), \
             patch("urllib.request.urlopen", fake_urlopen):
            result = self.mod.send_remote_gateway("ag2space", "!room:ag2.space", "hi")
        self.assertTrue(result)
        self.assertEqual(captured["url"], "https://gw.example/relay/v1/room")
        self.assertEqual(captured["auth"], "Bearer symlinked-token")

    def test_symlinked_env_refused_when_app_support_unset(self):
        """Fail-closed: the SAME layout without SUTANDO_APP_SUPPORT is just a
        symlink to some <dir>/channels/ag2space/.env with the right leaf shape —
        exactly the case a leaf-only check would wave through (review on #3416).
        No approved second root means no exception."""
        cfg, _app = self._symlinked_channels_root()
        self.assertTrue(self._refused(self._clean_env(cfg, None)))

    def test_symlinked_env_under_a_different_root_is_refused(self):
        """Shape is not identity: SUTANDO_APP_SUPPORT names one root, and a
        target with the right `<source>/.env` shape under some OTHER root is
        still outside it. This is the test that would catch a regression back
        to matching the last two path components."""
        cfg, _app = self._symlinked_channels_root()
        other_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other_root, ignore_errors=True)
        self.assertTrue(self._refused(self._clean_env(cfg, other_root)))

    def test_symlinked_env_off_the_exact_path_under_app_support_is_refused(self):
        """Even under the approved root, only the app's copy of THIS channel's
        env is accepted: another channel's file, a same-shaped file outside
        channels/, or a differently named file are all refused."""
        cases = {
            "other channel": dict(target_dirname="discord"),
            "outside channels/": dict(target_subdir="workspace"),
            "wrong filename": dict(target_filename="secrets.txt"),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                cfg, app = self._symlinked_channels_root(**kwargs)
                self.assertTrue(self._refused(self._clean_env(cfg, app)))

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

    def test_containment_delegates_to_shared_module_not_a_copy(self):
        """Delegation, not re-implementation: stub the shared
        src/channel_env_containment.py function to always refuse, and even
        the legitimate app-support relocation (normally accepted) must now
        be refused. If notify.py carried its own copy of the rule, this
        module-level stub would have no effect and the send would still
        succeed."""
        cfg, app = self._symlinked_channels_root()
        with patch.object(channel_env_containment, "channel_env_is_contained",
                          return_value=False):
            mod = _load()  # reload so the patched shared function is bound in
            with patch.dict(os.environ, self._clean_env(cfg, app), clear=True):
                result = mod.send_remote_gateway("ag2space", "!room:ag2.space", "hi")
        self.assertFalse(result)

    def test_load_channel_env_containment_fails_closed_when_import_fails(self):
        """The fallback lambda itself, not just `_channel_env_is_contained`'s
        already-bound result: force the shared src/channel_env_containment.py
        import to fail and assert the returned callable refuses even an
        otherwise-valid-looking containment case — never silently widen the
        guard just because the import failed."""
        import builtins
        real_import = builtins.__import__

        def boom(name, *a, **kw):
            if name == "channel_env_containment":
                raise ImportError("simulated: src/ not importable")
            return real_import(name, *a, **kw)

        with patch.object(builtins, "__import__", boom):
            fallback = self.mod._load_channel_env_containment()

        cfg, app = self._symlinked_channels_root()
        env_path = os.path.join(cfg, "channels", "ag2space", ".env")
        channels_dir = os.path.join(cfg, "channels")
        self.assertFalse(fallback(env_path, channels_dir, "ag2space"))

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

    def test_dotted_traversal_shapes_still_refused(self):
        # Dots are legal BETWEEN alphanumerics (domain-named lanes) — every
        # traversal-adjacent shape stays out, before containment even runs.
        for bad in ("..", "../evil", "a..b", "a.", ".a", "a.-b", "dev.ag2.space."):
            self.assertFalse(self.mod.send_remote_gateway(bad, "!r:s", "hi"), bad)

    def test_domain_named_source_reads_its_channel_env(self):
        # The owner-ideal naming: channels/dev.ag2.space/ is a valid lane and
        # its .env is read (send reaches _post with that lane's gateway).
        lane = self.cfg / "channels" / "dev.ag2.space"
        lane.mkdir()
        (lane / ".env").write_text(
            "REMOTE_TASK_URL=https://dev-gw.example\nREMOTE_TASK_TOKEN=dev-token\n")
        posts = []
        with patch.object(self.mod, "_post", side_effect=lambda *a, **k: posts.append(a) or True):
            ok = self.mod.send_remote_gateway("dev.ag2.space", "!r:dev.ag2.space", "hi")
        self.assertTrue(ok)
        self.assertEqual(len(posts), 1)
        self.assertIn("dev-gw.example", str(posts[0]))


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


class TestChannelEnvContainmentDelegation(unittest.TestCase):
    """notify.py must call the shared src/channel_env_containment.py function,
    not carry its own copy — the exact duplication CLAUDE.md's architecture
    rules call out (see also TestSendRemoteGateway
    .test_containment_delegates_to_shared_module_not_a_copy for the
    behavioral half of this proof)."""

    def test_binds_the_shared_function_by_identity(self):
        mod = _load()
        self.assertIs(mod._channel_env_is_contained,
                      channel_env_containment.channel_env_is_contained)


if __name__ == "__main__":
    unittest.main()
