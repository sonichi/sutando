#!/usr/bin/env python3
"""
Unit + structural tests for skills/bot2bot-post/post.py.

Guards tested:
  1.  resolve_bot2bot_channel — explicit bot2bot role tag wins
  2.  resolve_bot2bot_channel — fallback to first `true` group (legacy)
  3.  resolve_bot2bot_channel — exits when no channel found
  4.  resolve_other_bot — returns other bot ID from channel allowFrom
  5.  resolve_other_bot — excludes self_id from candidates
  6.  resolve_other_bot — prefers channel-level allowFrom over top-level
  7.  resolve_other_bot — returns None when no other bot configured
  8.  resolve_other_bot — bot-vs-owner heuristic (not in global allowFrom)
  9.  VALID_KINDS contains exactly the 5 expected kinds
 10.  load_token — parses DISCORD_BOT_TOKEN correctly from .env
 11.  load_token — strips surrounding quotes from token value
 12.  load_token — exits when token key is absent from .env
 13.  Structural: ENV_FILE path under ~/.claude/channels/discord/
 14.  Structural: ACCESS_JSON path under ~/.claude/channels/discord/
 15.  Structural: message includes `kind: text` format

Run: python3 skills/bot2bot-post/tests/post.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent.parent.parent
SRC = REPO / "skills" / "bot2bot-post" / "post.py"
SOURCE = SRC.read_text(encoding="utf-8")


def _load():
    """Load post.py without executing main()."""
    spec = importlib.util.spec_from_file_location("bot2bot_post", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()
resolve_channel = mod.resolve_bot2bot_channel
resolve_other = mod.resolve_other_bot
VALID_KINDS = mod.VALID_KINDS


class TestResolveBotToBot(unittest.TestCase):

    # ── 1. Explicit bot2bot role tag wins ───────────────────────────────────
    def test_explicit_role_tag_wins(self):
        access = {
            "groups": {
                "111": True,
                "222": {"role": "bot2bot", "allowFrom": []},
            }
        }
        self.assertEqual(resolve_channel(access), "222")

    # ── 2. Fallback to first `true` group ───────────────────────────────────
    def test_fallback_to_true_group(self):
        access = {"groups": {"999": True}}
        self.assertEqual(resolve_channel(access), "999")

    # ── 3. SystemExit when no channel found ─────────────────────────────────
    def test_exits_when_no_channel(self):
        access = {"groups": {}}
        with self.assertRaises(SystemExit):
            resolve_channel(access)

    def test_exits_when_groups_missing(self):
        with self.assertRaises(SystemExit):
            resolve_channel({})


class TestResolveOtherBot(unittest.TestCase):

    # ── 4. Returns other bot ID from channel allowFrom ──────────────────────
    def test_returns_other_bot_from_channel(self):
        access = {
            "groups": {
                "chan1": {"role": "bot2bot", "allowFrom": ["self123", "other456"]}
            },
            "allowFrom": ["owner999"],
        }
        result = resolve_other(access, "self123", "chan1")
        self.assertEqual(result, "other456")

    # ── 5. Excludes self_id ─────────────────────────────────────────────────
    def test_excludes_self_id(self):
        access = {
            "groups": {"ch": {"allowFrom": ["self1", "other2"]}},
            "allowFrom": [],
        }
        result = resolve_other(access, "self1", "ch")
        self.assertNotEqual(result, "self1")
        self.assertEqual(result, "other2")

    # ── 6. Channel-level allowFrom preferred over top-level ─────────────────
    def test_channel_allowfrom_preferred(self):
        access = {
            "groups": {"ch": {"allowFrom": ["self", "sibling-bot"]}},
            "allowFrom": ["self", "legacy-bot"],  # top-level has different bot
        }
        result = resolve_other(access, "self", "ch")
        # sibling-bot is not in global allowFrom → preferred by heuristic
        self.assertEqual(result, "sibling-bot")

    # ── 7. Returns None when no other bot configured ─────────────────────────
    def test_returns_none_when_no_other_bot(self):
        access = {
            "groups": {"ch": {"allowFrom": ["self"]}},
            "allowFrom": [],
        }
        result = resolve_other(access, "self", "ch")
        self.assertIsNone(result)

    # ── 8. Bot-vs-owner heuristic ────────────────────────────────────────────
    def test_prefers_id_not_in_global_allowfrom(self):
        """Owner ID appears in top-level allowFrom; sibling bot does not.
        Should prefer the non-owner ID."""
        access = {
            "groups": {"ch": {"allowFrom": ["self", "owner-id", "bot-id"]}},
            "allowFrom": ["owner-id"],  # owner is in global allowFrom
        }
        result = resolve_other(access, "self", "ch")
        self.assertEqual(result, "bot-id")

    # ── Fallback to top-level allowFrom for legacy configs ───────────────────
    def test_legacy_fallback_to_top_level_allowfrom(self):
        access = {
            "groups": {"ch": True},  # no channel-level allowFrom
            "allowFrom": ["self", "other-bot"],
        }
        result = resolve_other(access, "self", "ch")
        self.assertEqual(result, "other-bot")


class TestValidKinds(unittest.TestCase):

    # ── 9. VALID_KINDS contains exactly the 5 expected kinds ─────────────────
    def test_valid_kinds_set(self):
        expected = {"claim", "blocked", "done", "ping", "opinion"}
        self.assertEqual(VALID_KINDS, expected)

    def test_claim_in_valid_kinds(self):
        self.assertIn("claim", VALID_KINDS)

    def test_done_in_valid_kinds(self):
        self.assertIn("done", VALID_KINDS)

    def test_invalid_kind_not_in_set(self):
        for bad in ("update", "heartbeat", "status", "nack"):
            self.assertNotIn(bad, VALID_KINDS, f"{bad!r} should not be a valid kind")


class TestLoadToken(unittest.TestCase):

    # ── 10. Parses DISCORD_BOT_TOKEN correctly ───────────────────────────────
    def test_parses_token(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("DISCORD_BOT_TOKEN=my-test-token\n")
            tmp = Path(f.name)
        try:
            with patch.object(mod, "ENV_FILE", tmp):
                token = mod.load_token()
            self.assertEqual(token, "my-test-token")
        finally:
            tmp.unlink()

    # ── 11. Strips surrounding quotes from token ─────────────────────────────
    def test_strips_quotes_from_token(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write('DISCORD_BOT_TOKEN="quoted-token"\n')
            tmp = Path(f.name)
        try:
            with patch.object(mod, "ENV_FILE", tmp):
                token = mod.load_token()
            self.assertEqual(token, "quoted-token")
        finally:
            tmp.unlink()

    # ── 12. Exits when token key absent ─────────────────────────────────────
    def test_exits_when_token_missing(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("OTHER_VAR=value\n")
            tmp = Path(f.name)
        try:
            with patch.object(mod, "ENV_FILE", tmp):
                with self.assertRaises(SystemExit):
                    mod.load_token()
        finally:
            tmp.unlink()

    def test_exits_when_env_file_missing(self):
        missing = Path("/tmp/nonexistent-bot2bot-test-env-file.env")
        with patch.object(mod, "ENV_FILE", missing):
            with self.assertRaises(SystemExit):
                mod.load_token()


class TestSourceStructure(unittest.TestCase):

    # ── 13. ENV_FILE path ────────────────────────────────────────────────────
    def test_env_file_path_in_discord_channel_dir(self):
        self.assertIn('".claude" / "channels" / "discord" / ".env"', SOURCE,
                      "ENV_FILE must point to ~/.claude/channels/discord/.env")

    # ── 14. ACCESS_JSON path ─────────────────────────────────────────────────
    def test_access_json_path(self):
        self.assertIn('"access.json"', SOURCE,
                      "ACCESS_JSON must point to the discord channel access.json")

    # ── 15. Message format includes `kind: text` ─────────────────────────────
    def test_message_format_includes_kind_text(self):
        self.assertRegex(SOURCE, r'f".*\{kind\}.*:\s*\{text\}|f".*kind.*:.*text',
                         "Message must include `kind: text` format")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [TestResolveBotToBot, TestResolveOtherBot, TestValidKinds,
                TestLoadToken, TestSourceStructure]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
