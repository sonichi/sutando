#!/usr/bin/env python3
"""Behavioral tests for empty Slack @mention context recovery.

Run: python3 tests/slack-bridge-empty-mention-context.test.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Hermetic isolation (enforced by scripts/lint-hermetic-bridge-tests.py): the Slack
# bridge resolves channel config at MODULE level during exec_module, so this must
# isolate CLAUDE_CONFIG_DIR to a temp dir AND seed the canonical slack access.json
# BEFORE the bridge is imported — otherwise `channel_access_path("slack")` falls back
# to the operator's real per-user allowlist. Must run at module scope and before the
# import (the lint checks both order and that the value is a real temp dir).
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-slack-empty-mention-")
_cfg_slack = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_cfg_slack.mkdir(parents=True, exist_ok=True)
(_cfg_slack / "access.json").write_text('{"allowFrom": []}')


def _load_bridge():
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-token"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-token"
    os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix="slack-empty-mention-")
    os.environ["SUTANDO_TEST_MODE"] = "1"

    class StubApp:
        def __init__(self, *args, **kwargs):
            self.client = types.SimpleNamespace()

        def event(self, _name):
            return lambda fn: fn

    bolt = types.ModuleType("slack_bolt")
    bolt.App = StubApp
    sys.modules["slack_bolt"] = bolt
    sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
    socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket_mode.SocketModeHandler = object
    sys.modules["slack_bolt.adapter.socket_mode"] = socket_mode
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location(
        "slack_bridge_empty_mention", REPO / "src" / "slack-bridge.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BRIDGE = _load_bridge()


def mention_event(text="<@UBOT>", *, thread_ts="1700000000.000001"):
    event = {
        "user": "UOWNER",
        "channel": "CDEV",
        "text": text,
        "ts": "1700000002.000003",
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return event


class EmptyMentionContextTest(unittest.TestCase):
    def setUp(self):
        self.captured = []
        self.original_write = BRIDGE._write_task
        self.original_username = BRIDGE._resolve_username
        BRIDGE._write_task = lambda event, prefix, text, username: self.captured.append(
            (event, prefix, text, username)
        )
        BRIDGE._resolve_username = lambda _uid: "Rui"

    def tearDown(self):
        BRIDGE._write_task = self.original_write
        BRIDGE._resolve_username = self.original_username

    def test_nonempty_mention_keeps_existing_behavior(self):
        BRIDGE.app.client.conversations_replies = lambda **kwargs: self.fail(
            "nonempty mentions must not fetch thread history"
        )
        BRIDGE.app.client.conversations_history = lambda **kwargs: self.fail(
            "nonempty mentions must not fetch channel history"
        )
        BRIDGE.handle_mention(mention_event("<@UBOT> do the task"), None)
        self.assertEqual(self.captured[0][1:], ("Slack mention", "do the task", "Rui"))

    def test_empty_mention_recovers_latest_same_sender_thread_message(self):
        BRIDGE.app.client.conversations_replies = lambda **kwargs: {
            "messages": [
                {"ts": "1700000000.000001", "user": "UOTHER", "text": "competitor list"},
                {"ts": "1700000001.000002", "user": "UOWNER", "text": "Run it now"},
                {"ts": "1700000002.000003", "user": "UOWNER", "text": "<@UBOT>"},
            ]
        }
        BRIDGE.handle_mention(mention_event(), None)
        self.assertEqual(
            self.captured[0][1:],
            ("Slack mention (recovered prior message)", "Run it now", "Rui"),
        )

    def test_recovered_text_preserves_mentions_inside_the_task(self):
        BRIDGE.app.client.conversations_replies = lambda **kwargs: {
            "messages": [
                {
                    "ts": "1700000001.000002",
                    "user": "UOWNER",
                    "text": "Ask <@UCOLLAB> to review it",
                }
            ]
        }
        BRIDGE.handle_mention(mention_event(), None)
        self.assertEqual(self.captured[0][2], "Ask <@UCOLLAB> to review it")

    def test_recovery_ignores_other_users_bots_and_mention_only_turns(self):
        BRIDGE.app.client.conversations_replies = lambda **kwargs: {
            "messages": [
                {"ts": "1700000000.000001", "user": "UOTHER", "text": "delete everything"},
                {"ts": "1700000000.500001", "user": "UOWNER", "text": "<@UBOT>"},
                {"ts": "1700000001.000002", "user": "UOWNER", "bot_id": "B1", "text": "bot text"},
            ]
        }
        BRIDGE.handle_mention(mention_event(), None)
        self.assertEqual(self.captured[0][1], "Slack mention")
        self.assertEqual(self.captured[0][2], BRIDGE._EMPTY_MENTION_CLARIFICATION)

    def test_bot_reply_is_a_boundary_not_crossed(self):
        # CR #2230: owner "delete X" → bot "done" → bare @Sutando must NOT recover
        # and re-run "delete X". The bot reply is a conversation boundary proving
        # the prior owner turn was already answered → clarification, not re-run.
        BRIDGE.app.client.conversations_replies = lambda **kwargs: {
            "messages": [
                {"ts": "1700000000.000001", "user": "UOWNER", "text": "delete the prod database"},
                {"ts": "1700000001.000002", "bot_id": "B1", "text": "Done — deleted it."},
                {"ts": "1700000002.000003", "user": "UOWNER", "text": "<@UBOT>"},
            ]
        }
        BRIDGE.handle_mention(mention_event(), None)
        self.assertEqual(self.captured[0][1], "Slack mention")
        self.assertEqual(self.captured[0][2], BRIDGE._EMPTY_MENTION_CLARIFICATION)

    def test_split_turn_still_recovers_when_no_bot_between(self):
        # The legitimate split turn (owner instruction immediately before the
        # mention, no bot reply between) MUST still recover — the fix only stops
        # at a bot boundary, it does not disable recovery. An older bot message
        # before the instruction is never reached.
        BRIDGE.app.client.conversations_replies = lambda **kwargs: {
            "messages": [
                {"ts": "1700000000.000001", "bot_id": "B0", "text": "earlier unrelated bot msg"},
                {"ts": "1700000001.000002", "user": "UOWNER", "text": "Run it now"},
                {"ts": "1700000002.000003", "user": "UOWNER", "text": "<@UBOT>"},
            ]
        }
        BRIDGE.handle_mention(mention_event(), None)
        self.assertEqual(
            self.captured[0][1:],
            ("Slack mention (recovered prior message)", "Run it now", "Rui"),
        )

    def test_history_error_falls_back_to_clarification(self):
        def fail(**kwargs):
            raise RuntimeError("Slack unavailable")

        BRIDGE.app.client.conversations_replies = fail
        BRIDGE.handle_mention(mention_event(), None)
        self.assertEqual(self.captured[0][2], BRIDGE._EMPTY_MENTION_CLARIFICATION)

    def test_top_level_empty_mention_recovers_latest_same_sender_message(self):
        BRIDGE.app.client.conversations_replies = lambda **kwargs: self.fail(
            "top-level mention must use channel history"
        )
        captured = []
        BRIDGE.app.client.conversations_history = lambda **kwargs: (
            captured.append(kwargs)
            or {
                "messages": [
                    {"ts": "1700000001.000002", "user": "UOWNER", "text": "Run it now"},
                    {"ts": "1700000000.000001", "user": "UOTHER", "text": "older"},
                ]
            }
        )
        BRIDGE.handle_mention(mention_event(thread_ts=None), None)
        self.assertEqual(
            captured,
            [{"channel": "CDEV", "latest": "1700000002.000003", "inclusive": False, "limit": 100}],
        )
        self.assertEqual(
            self.captured[0][1:],
            ("Slack mention (recovered prior message)", "Run it now", "Rui"),
        )

    def test_top_level_empty_mention_does_not_cross_another_human(self):
        BRIDGE.app.client.conversations_history = lambda **kwargs: {
            "messages": [
                {"ts": "1700000001.500002", "user": "UOTHER", "text": "intervening"},
                {"ts": "1700000001.000002", "user": "UOWNER", "text": "delete it"},
            ]
        }
        BRIDGE.handle_mention(mention_event(thread_ts=None), None)
        self.assertEqual(self.captured[0][2], BRIDGE._EMPTY_MENTION_CLARIFICATION)

    def test_served_bare_mention_is_a_boundary_not_walked_past(self):
        # CR #2230 (bassilkhilo-ag2), exact repro: "delete X" → a bare @Sutando
        # (already served) → a NEW bare @Sutando must NOT walk past the served
        # mention and re-recover/re-run "delete X". No bot message sits between
        # them, so ONLY the mention-only boundary can stop the walk here (the
        # pre-existing bot-reply boundary does not apply).
        BRIDGE.app.client.conversations_replies = lambda **kwargs: {
            "messages": [
                {"ts": "1700000000.000001", "user": "UOWNER", "text": "delete the prod database"},
                {"ts": "1700000001.000002", "user": "UOWNER", "text": "<@UBOT>"},
                {"ts": "1700000002.000003", "user": "UOWNER", "text": "<@UBOT>"},
            ]
        }
        BRIDGE.handle_mention(mention_event(), None)
        self.assertEqual(self.captured[0][1], "Slack mention")
        self.assertEqual(self.captured[0][2], BRIDGE._EMPTY_MENTION_CLARIFICATION)

    def test_recovery_ignores_a_stale_same_user_instruction(self):
        # CR #2230: a same-user instruction older than the recovery window is not
        # the other half of a split turn — it must not resurface "as if fresh".
        stale_ts = float(mention_event()["ts"]) - (BRIDGE._EMPTY_MENTION_RECOVERY_MAX_AGE_S + 60)
        BRIDGE.app.client.conversations_replies = lambda **kwargs: {
            "messages": [
                {"ts": f"{stale_ts:.6f}", "user": "UOWNER", "text": "delete the prod database"},
                {"ts": "1700000002.000003", "user": "UOWNER", "text": "<@UBOT>"},
            ]
        }
        BRIDGE.handle_mention(mention_event(), None)
        self.assertEqual(self.captured[0][2], BRIDGE._EMPTY_MENTION_CLARIFICATION)

    def test_unparseable_timestamp_fails_closed(self):
        # An unparseable ts means the age is UNKNOWN, and recovered text becomes a
        # live task — so recovery must stop and ask, not proceed. Fail-open here
        # would let the one input the recency bound cannot evaluate bypass it.
        BRIDGE.app.client.conversations_replies = lambda **kwargs: {
            "messages": [
                {"ts": "", "user": "UOWNER", "text": "Run it now"},
                {"ts": "1700000002.000003", "user": "UOWNER", "text": "<@UBOT>"},
            ]
        }
        BRIDGE.handle_mention(mention_event(), None)
        self.assertEqual(self.captured[0][2], BRIDGE._EMPTY_MENTION_CLARIFICATION)

    def test_unparseable_timestamp_does_not_resurface_destructive_text(self):
        # Non-empty but non-numeric ts, and it must sort BELOW the mention's ts:
        # the `message["ts"] >= current_ts` guard above is a STRING compare, so a
        # malformed ts sorting high is skipped before the parse is ever reached.
        BRIDGE.app.client.conversations_replies = lambda **kwargs: {
            "messages": [
                {"ts": "1700000001.bad", "user": "UOWNER", "text": "delete the prod database"},
                {"ts": "1700000002.000003", "user": "UOWNER", "text": "<@UBOT>"},
            ]
        }
        BRIDGE.handle_mention(mention_event(), None)
        self.assertEqual(self.captured[0][2], BRIDGE._EMPTY_MENTION_CLARIFICATION)
        self.assertNotIn("delete the prod database", str(self.captured))

    def test_recovery_within_window_still_succeeds(self):
        # Positive control: an instruction well within the window (2 min before
        # the mention) still recovers — the recency bound only rejects stale ones.
        recent_ts = float(mention_event()["ts"]) - 120
        BRIDGE.app.client.conversations_replies = lambda **kwargs: {
            "messages": [
                {"ts": f"{recent_ts:.6f}", "user": "UOWNER", "text": "Run it now"},
                {"ts": "1700000002.000003", "user": "UOWNER", "text": "<@UBOT>"},
            ]
        }
        BRIDGE.handle_mention(mention_event(), None)
        self.assertEqual(
            self.captured[0][1:],
            ("Slack mention (recovered prior message)", "Run it now", "Rui"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
