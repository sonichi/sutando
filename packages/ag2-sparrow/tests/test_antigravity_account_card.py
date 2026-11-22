#!/usr/bin/env python3
"""Antigravity's account card (adapters/antigravity/agy_account.py) in the owner's DM: 切换账号 and 用 API key
point to the agent's settings (no code or key is ever taken in chat), 停用 API key removes the key, the turn
after a switch says which account agy now uses, and a quota error shows the card.

Driven through the connector against tests/agy_stub_acp.py, with the Keychain faked, dummy codes and keys only.

Run:
    python3 -m pytest -q packages/ag2-sparrow/tests/test_antigravity_account_card.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_antigravity_adapter as ta  # noqa: E402  (its hermetic env and Work)

from ag2_sparrow import account_switch, connector_cards  # noqa: E402
from ag2_sparrow.adapters.antigravity import agy_account, antigravity_connector as gc, gemini_key as gk  # noqa: E402

CODE = "4/0A" + "d" * 40                # shaped like a Google sign-in code; not a real one
DUMMY_KEY = "AIza" + "D" * 35            # shaped like a Google API key; not a real one
AGENT = "@agy-bot:s.space"
DM = "!dm:s.space"


def setUpModule():
    ta.setUpModule()


def tearDownModule():
    ta.tearDownModule()


class AccountCardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = ta.Work(self.tmp.name)
        self.keychain = {"saved": False}
        self.cleared, self.logs = [], []
        self.connector = None
        patches = [
            mock.patch.object(gk, "key_saved", lambda agent_id, **kw: agent_id == AGENT and self.keychain["saved"]),
            mock.patch.object(gk, "clear_key", self.fake_clear),
            mock.patch.object(gk, "gemini_api_key_from_app", lambda agent_id="", **kw: ""),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def fake_clear(self, agent_id, **kw):
        self.cleared.append(agent_id)
        self.keychain["saved"] = False
        return True

    def serve(self):
        """One task on one long-lived connector, its log kept."""
        a = self.w.args("--agent-id", AGENT)
        if self.connector is None:
            self.connector = gc.Connector(a)
        else:
            self.connector.args = a
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.connector.serve()
        self.logs.append(err.getvalue())
        return err.getvalue()

    def dm(self, tid, body, reply_to="", click="", tier="owner", collaborator=False, kind="dm"):
        lines = ["id: " + tid, "timestamp: 2026-11-22T00:00:00Z", "source: ag2space", "channel_id: " + DM,
                 "channel_kind: " + kind, "source_message_id: $m-" + tid, "user_id: @alice:s.space",
                 "access_tier: " + tier] + (["collaborator: true"] if collaborator else [])
        if reply_to:
            lines.append("reply_to_event: " + reply_to)
        if click:
            lines.append("card_click: " + json.dumps({"card_id": account_switch.CARD, "action": click}))
        (self.w.root / "tasks" / (tid + ".txt")).write_text("\n".join(lines + ["task: " + body]) + "\n",
                                                            encoding="utf-8")

    def posted(self, event="$card1"):
        sent = []
        connector_cards.Poster(Path(str(self.w.root) + ".bridge") / "state",
                               lambda p: sent.append(p) or {"ok": True, "event_id": event}, log=lambda line: None).sweep()
        return [s["extra_content"]["space.ag2.card"] for s in sent]

    def menu(self, ask="换账号", event="$card1"):
        self.dm("task-ask", ask)
        self.serve()
        self.assertEqual(self.w.result("task-ask"), account_switch.ANSWERED)
        (card,) = self.posted(event)
        return card

    def tap(self, tid, card, label, event="$card1", **kw):
        said = next(b["reply"] for b in card["buttons"] if b["label"] == label)
        self.dm(tid, said, reply_to=event, click=said, **kw)
        return self.serve()

    def prompts(self):
        return [r for r in self.w.records() if r.get("method") == "session/prompt"]

    def everything_written(self):
        """Every file the connector or the bridge queue holds, and every log line: where a secret must not be."""
        out = list(self.logs)
        for top in (self.w.root, Path(str(self.w.root) + ".bridge")):
            for p in top.rglob("*"):
                if p.is_file():
                    out.append(p.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(out)

    # ── A1 ──
    def test_asking_in_words_shows_the_card_with_switch_and_api_key(self):
        for n, ask in enumerate(("换账号", "switch account", "用 API key", "/account")):
            self.dm("task-%d" % n, ask)
            self.serve()
            (card,) = self.posted("$c%d" % n)
            self.assertEqual([b["label"] for b in card["buttons"]], ["切换账号", "用 API key"], ask)
        self.assertEqual(self.prompts(), [], "asking for the card never reaches agy")

    # ── A2 ──
    def test_switch_points_to_the_settings_sign_in_and_starts_nothing_here(self):
        card = self.menu()
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("nothing is run")):
            self.tap("task-switch", card, "切换账号")
        (info,) = self.posted("$card2")
        self.assertIn("Connections", info["text"])
        self.assertIn("Switch account", info["text"])
        self.assertIn("不要把授权码发在这个对话里", info["text"])
        self.assertIn(agy_account.SHARED, info["text"])
        self.assertFalse(info.get("buttons"), "no input box and no link: no secret travels through the card")
        self.assertEqual(self.prompts(), [])

    def test_a_sign_in_code_sent_in_chat_is_never_taken_nor_passed_on(self):
        card = self.menu()
        self.tap("task-switch", card, "切换账号")
        self.posted("$card2")
        for n, body in enumerate((CODE, "http://localhost:51121/oauth2callback?state=s1&code=%s&scope=x" % CODE)):
            self.dm("task-code%d" % n, body)
            self.serve()
            self.assertIn("请把它从对话里删掉", self.w.result("task-code%d" % n))
        self.assertEqual(self.prompts(), [], "the code never reaches agy")
        self.assertEqual(self.posted("$x"), [], "and nothing acts on it")
        self.assertNotIn(CODE, "\n".join(self.logs) + json.dumps(self.w.metrics()) + self.state_text())

    def state_text(self):
        return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in (self.w.root / "state").rglob("*") if p.is_file())

    def test_the_turn_after_a_switch_says_which_account_agy_now_uses(self):
        (self.w.home / "account").write_text("alice@example.com")
        self.dm("task-1", "STUB:TEXT hi")
        self.serve()
        self.assertEqual(self.w.result("task-1"), "hello from agy-stub\n", "the first account seen is only kept")
        (self.w.home / "account").write_text("bob@example.com")
        self.dm("task-2", "STUB:TEXT hi")
        self.serve()
        self.assertTrue(self.w.result("task-2").startswith(gc.quoted("（已切到 b…@example.com 账号。）").strip()))
        self.assertIn("hello from agy-stub", self.w.result("task-2"))
        self.dm("task-3", "STUB:TEXT hi")
        self.serve()
        self.assertEqual(self.w.result("task-3"), "hello from agy-stub\n")
        card = self.menu()
        self.assertIn("b…@example.com", card["text"])
        self.assertNotIn("bob@example.com", self.state_text())

    def test_a_tap_that_is_not_the_owners_changes_nothing(self):
        self.keychain["saved"] = True
        card = self.menu()
        self.tap("task-team", card, "停用 API key", tier="team", collaborator=True)
        self.assertIn("couldn't match that button", self.w.result("task-team"))
        self.tap("task-guest", card, "切换账号", tier="guest")
        self.assertEqual((self.cleared, self.posted("$x")), ([], []))

    # ── A3 ──
    def test_api_key_points_to_the_settings_and_a_key_sent_here_is_never_passed_on(self):
        card = self.menu()
        self.tap("task-key", card, "用 API key")
        (info,) = self.posted("$card2")
        self.assertIn("Connections", info["text"])
        for n, body in enumerate(("my key " + DUMMY_KEY, "my key [REDACTED-Google API Key]")):
            self.dm("task-paste%d" % n, body)
            self.serve()
            self.assertIn("没有转给 Antigravity", self.w.result("task-paste%d" % n))
        self.assertEqual(self.prompts(), [])
        self.assertNotIn(DUMMY_KEY, "\n".join(self.logs) + json.dumps(self.w.metrics()))

    def test_with_a_saved_key_stop_removes_only_this_agents_item(self):
        self.keychain["saved"] = True
        card = self.menu()
        self.assertIn("Gemini API key", card["text"])
        self.assertEqual([b["label"] for b in card["buttons"]], ["切换账号", "用 API key", "停用 API key"])
        self.tap("task-stop", card, "停用 API key")
        self.assertEqual(self.cleared, [AGENT])
        (done,) = self.posted("$card2")
        self.assertEqual(done["state"], "done")

    # ── A4 ──
    def test_a_quota_error_shows_the_card_in_the_dm_once(self):
        self.dm("task-q1", "STUB:QUOTA hi")
        self.serve()
        (card,) = self.posted("$q1")
        self.assertIn("额度用完了", card["text"])
        self.assertEqual([b["label"] for b in card["buttons"]], ["切换账号", "用 API key"])
        self.dm("task-q2", "STUB:QUOTA again")
        self.serve()
        self.assertEqual(self.posted("$q2"), [], "at most once per QUOTA_EVERY_S")

    def test_a_quota_error_in_a_room_says_where_to_switch(self):
        self.dm("task-q", "STUB:QUOTA hi", kind="room")
        self.serve()
        self.assertIn("在和我的私信里说「换账号」", self.w.result("task-q"))
        self.assertEqual(self.posted("$q"), [])


class PartsTest(unittest.TestCase):
    def test_carries_secret_finds_codes_keys_and_markers_only(self):
        for said in (CODE, "here " + CODE, "http://localhost:1/cb?code=4/0Ab12345&state=x", "k " + DUMMY_KEY,
                     "[REDACTED-Google API Key]"):
            self.assertTrue(agy_account.carries_secret(said), said)
        for said in ("hello there", "4/5 of the tests pass", "http://example.com/no-code", "switch account"):
            self.assertFalse(agy_account.carries_secret(said), said)

    def test_signed_in_as_reads_only_this_processes_log(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "acp-server-1-42.log").write_text("x\nI1 server_oauth.go:221] applyAuthResult: email=bob@example.com, "
                                                       "authMethod=consumer, quotaProject=\n")
            Path(d, "acp-server-1-43.log").write_text("applyAuthResult: email=eve@example.com, authMethod=consumer\n")
            self.assertEqual(agy_account.signed_in_as(d, 42), "b…@example.com")
            self.assertEqual(agy_account.signed_in_as(d, 44), "")
            self.assertEqual(agy_account.signed_in_as(os.path.join(d, "none"), 42), "")

    def test_asks_for_card_only_for_short_asks(self):
        for ask in ("换账号", "帮我切换账号", "Switch account.", "use an API key", "/account"):
            self.assertTrue(agy_account.asks_for_card(ask), ask)
        self.assertFalse(agy_account.asks_for_card("write a script that can switch account for my users"))

    def test_key_saved_asks_for_attributes_only_and_clear_names_this_agents_item(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0 if argv[1] == "delete-generic-password" else 44, "", "")
        self.assertFalse(gk.key_saved(AGENT, run=run, platform="darwin", environ={}))
        self.assertTrue(gk.clear_key(AGENT, run=run, platform="darwin"))
        for argv in calls:
            self.assertNotIn("-w", argv)
            self.assertEqual(argv[argv.index("-s") + 1], gk.agent_service(AGENT))


if __name__ == "__main__":
    unittest.main()
