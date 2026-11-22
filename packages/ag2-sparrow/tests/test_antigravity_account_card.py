#!/usr/bin/env python3
"""Antigravity's account card (adapters/antigravity/agy_account.py), all in the owner's DM: 切换账号 runs
`agy acp --login` in the background and takes the authorization code from the owner's reply, 用 API key
points to the settings, 停用 API key removes the key, and a quota error shows the card.

Driven through the connector against tests/agy_stub_acp.py (the turns) and tests/agy_login_stub.py (the
sign-in), with the Keychain and the browser opener faked, dummy codes and keys only.

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

LOGIN_STUB = str(Path(__file__).resolve().parent / "agy_login_stub.py")
CODE = "DUMMY-CODE-1234"                 # the login stub's one good code
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
        self.cleared, self.opened, self.logs = [], [], []
        self.connector = None
        patches = [
            mock.patch.object(gk, "key_saved", lambda agent_id, **kw: agent_id == AGENT and self.keychain["saved"]),
            mock.patch.object(gk, "clear_key", self.fake_clear),
            mock.patch.object(gk, "gemini_api_key_from_app", lambda agent_id="", **kw: ""),
            mock.patch.object(gc, "open_page", lambda url: self.opened.append(url) or True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        if self.connector is not None:
            self.connector.accounts.give_up()
        self.tmp.cleanup()

    def fake_clear(self, agent_id, **kw):
        self.cleared.append(agent_id)
        self.keychain["saved"] = False
        return True

    def serve(self):
        """One task on one long-lived connector (a waiting sign-in lives in it), its log kept."""
        a = self.w.args("--agent-id", AGENT)
        if self.connector is None:
            self.connector = gc.Connector(a)
            self.connector.accounts.login_argv = [sys.executable, LOGIN_STUB]
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

    def switch(self):
        card = self.menu()
        self.tap("task-switch", card, "切换账号")
        (info,) = self.posted("$card2")
        return info

    # ── A1 ──
    def test_asking_in_words_shows_the_card_with_switch_and_api_key(self):
        for n, ask in enumerate(("换账号", "switch account", "用 API key", "/account")):
            self.dm("task-%d" % n, ask)
            self.serve()
            (card,) = self.posted("$c%d" % n)
            self.assertEqual([b["label"] for b in card["buttons"]], ["切换账号", "用 API key"], ask)
        self.assertEqual(self.prompts(), [], "asking for the card never reaches agy")

    # ── A2 ──
    def test_switch_posts_the_link_and_the_code_sent_back_finishes_the_sign_in_unseen(self):
        info = self.switch()
        url = next(b["url"] for b in info["buttons"] if b["label"] == "打开登录页")
        self.assertTrue(url.startswith("https://accounts.google.com/"))
        self.assertEqual(self.opened, [url])
        self.assertIn(agy_account.SHARED, info["text"])
        self.dm("task-code", CODE)
        self.serve()
        (done,) = self.posted("$card3")
        self.assertEqual(done["state"], "done")
        self.assertIn("已切到 a…@example.com 账号", done["text"])
        self.assertEqual(self.w.result("task-code"), account_switch.ANSWERED)
        self.assertEqual(self.prompts(), [], "the code never reaches agy's turn")
        self.assertEqual(self.w.metrics()[-1]["card"], "account-switched")
        self.assertNotIn(CODE, self.everything_written())
        self.assertFalse(self.connector.accounts.waiting())

    def test_the_whole_landing_address_works_too(self):
        self.switch()
        self.dm("task-code", "http://localhost:51121/oauth2callback?state=s1&code=%s&scope=email" % CODE)
        self.serve()
        (done,) = self.posted("$card3")
        self.assertEqual(done["state"], "done")
        self.assertNotIn(CODE, self.everything_written())

    def test_a_wrong_code_says_so_and_offers_the_card_again(self):
        self.switch()
        self.dm("task-code", "DUMMY-WRONG-9999")
        self.serve()
        (again,) = self.posted("$card3")
        self.assertIn("登录没有完成", again["text"])
        self.assertEqual([b["label"] for b in again["buttons"]], ["切换账号", "用 API key"])
        self.assertNotIn("DUMMY-WRONG-9999", self.everything_written())

    def test_while_waiting_ordinary_words_and_other_senders_are_not_taken_as_the_code(self):
        self.switch()
        self.dm("task-words", "STUB:TEXT what is the weather")
        self.serve()
        self.assertEqual(self.w.result("task-words"), "hello from agy-stub\n")
        self.dm("task-team", CODE, tier="team", collaborator=True)
        self.serve()
        self.assertTrue(self.connector.accounts.waiting(), "a collaborator's message never feeds the sign-in")
        self.assertEqual(self.posted("$x"), [])

    def test_a_tap_that_is_not_the_owners_changes_nothing(self):
        self.keychain["saved"] = True
        card = self.menu()
        self.tap("task-team", card, "停用 API key", tier="team", collaborator=True)
        self.assertIn("couldn't match that button", self.w.result("task-team"))
        self.tap("task-guest", card, "切换账号", tier="guest")
        self.assertEqual((self.cleared, self.opened, self.posted("$x")), ([], [], []))
        self.assertIsNone(self.connector.accounts.login)

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
    def test_code_in_takes_a_code_or_an_addresses_code_and_nothing_else(self):
        self.assertEqual(agy_account.code_in(" 4/0AbCd-123_xyz "), "4/0AbCd-123_xyz")
        self.assertEqual(agy_account.code_in("http://localhost:1/cb?code=4/0Ab12345&state=x"), "4/0Ab12345")
        for words in ("hello there", "thanks!", "http://example.com/no-code", "abcdefghij"):
            self.assertEqual(agy_account.code_in(words), "", words)

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
