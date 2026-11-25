#!/usr/bin/env python3
"""Antigravity's account card (adapters/antigravity/agy_account.py) in the owner's DM: "Switch account" and "Use API key"
are done in the card on the computer that runs the agent (no code or key is ever taken in chat), "Stop using API key" removes the key, the turn
after a switch says which account agy now uses, and a quota error shows the card in the owner's DM once per limit.

Driven through the connector against tests/agy_stub_acp.py, with the Keychain faked, dummy codes and keys only.

Run:
    python3 -m pytest -q packages/ag2-sparrow/tests/test_antigravity_account_card.py
"""
import contextlib
import io
import json
import os
import re
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
        if self.keychain.get("broken"):
            return gk.FAILED
        was = self.keychain["saved"]
        self.keychain["saved"] = False
        return gk.REMOVED if was else gk.ABSENT

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
            self.assertEqual([b["label"] for b in card["buttons"]], ["Switch account", "Use API key"], ask)
        self.assertEqual(self.prompts(), [], "asking for the card never reaches agy")

    def test_the_card_is_english_and_carries_the_marker_for_the_in_card_controls(self):
        card = self.menu()
        self.assertEqual(card["account_controls"], {"runtime": "antigravity", "agent": AGENT})
        self.assertEqual(card["header"]["title"], "Antigravity account")
        self.assertTrue(card["text"].endswith(agy_account.BELOW), card["text"])
        self.assertEqual(agy_account.elsewhere("Antigravity"),
                         "Open this on the computer that runs Antigravity to switch account.")
        self.tap("task-key", card, "Use API key")
        (info,) = self.posted("$card2")
        self.assertNotIn("account_controls", info, "a notice is not the account card")
        for said in (card, info):
            self.assertFalse(re.search(r"[\u4e00-\u9fff]", json.dumps(said, ensure_ascii=False)), said)

    # ── A2 ──
    def test_switch_is_done_in_the_card_and_starts_nothing_here(self):
        card = self.menu()
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("nothing is run")):
            self.tap("task-switch", card, "Switch account")
        (info,) = self.posted("$card2")
        self.assertIn(agy_account.elsewhere(gc.NAME), info["text"])
        self.assertNotIn("Settings", info["text"])
        self.assertIn("Never send a sign-in code in this chat", info["text"])
        self.assertIn(agy_account.SHARED, info["text"])
        self.assertFalse(info.get("buttons"), "no input box and no link: no secret travels through the card")
        self.assertEqual(self.prompts(), [])

    def test_a_sign_in_code_sent_in_chat_is_never_taken_nor_passed_on(self):
        card = self.menu()
        self.tap("task-switch", card, "Switch account")
        self.posted("$card2")
        for n, body in enumerate((CODE, "http://localhost:51121/oauth2callback?state=s1&code=%s&scope=x" % CODE)):
            self.dm("task-code%d" % n, body)
            self.serve()
            self.assertIn("Please delete it from the chat", self.w.result("task-code%d" % n))
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
        self.assertTrue(self.w.result("task-2").startswith(gc.quoted("(Switched to the b…@example.com account.)").strip()))
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
        self.tap("task-team", card, "Stop using API key", tier="team", collaborator=True)
        self.assertIn("couldn't match that button", self.w.result("task-team"))
        self.tap("task-guest", card, "Switch account", tier="guest")
        self.assertEqual((self.cleared, self.posted("$x")), ([], []))

    # ── A3 ──
    def test_api_key_is_entered_in_the_card_and_a_key_sent_here_is_never_passed_on(self):
        card = self.menu()
        self.tap("task-key", card, "Use API key")
        (info,) = self.posted("$card2")
        self.assertIn("Open this on the computer that runs Antigravity", info["text"])
        for n, body in enumerate(("my key " + DUMMY_KEY, "my key [REDACTED-Google API Key]")):
            self.dm("task-paste%d" % n, body)
            self.serve()
            self.assertIn("did not pass it to Antigravity", self.w.result("task-paste%d" % n))
        self.assertEqual(self.prompts(), [])
        self.assertNotIn(DUMMY_KEY, "\n".join(self.logs) + json.dumps(self.w.metrics()))

    def test_with_a_saved_key_stop_removes_only_this_agents_item(self):
        self.keychain["saved"] = True
        card = self.menu()
        self.assertIn("Gemini API key", card["text"])
        self.assertEqual([b["label"] for b in card["buttons"]], ["Switch account", "Use API key", "Stop using API key"])
        self.tap("task-stop", card, "Stop using API key")
        self.assertEqual(self.cleared, [AGENT])
        (done,) = self.posted("$card2")
        self.assertEqual(done["state"], "done")

    def test_a_keychain_failure_is_said_as_one_never_as_removed(self):
        self.keychain.update(saved=True, broken=True)
        card = self.menu()
        self.tap("task-stop", card, "Stop using API key")
        (said,) = self.posted("$card2")
        self.assertEqual(said["state"], "error")
        self.assertIn("could not delete the API key", said["text"])
        self.assertNotIn("Stopped using the API key", said["text"])
        self.assertEqual(self.w.metrics()[-1]["card"], "account-key-not-cleared")

    def test_stop_on_an_older_card_after_the_key_is_gone_changes_nothing_and_says_so(self):
        self.keychain["saved"] = True
        card = self.menu()
        self.tap("task-stop1", card, "Stop using API key")
        self.posted("$card2")
        self.tap("task-stop2", card, "Stop using API key")
        (said,) = self.posted("$card3")
        self.assertIn("has no saved API key", said["text"])
        self.assertNotIn("Stopped using the API key", said["text"])
        self.assertEqual(self.w.metrics()[-1]["card"], "account-key-absent")

    def test_the_switch_notice_is_said_only_in_the_owners_dm(self):
        (self.w.home / "account").write_text("alice@example.com")
        self.dm("task-1", "STUB:TEXT hi")
        self.serve()
        (self.w.home / "account").write_text("bob@example.com")
        self.dm("task-room", "STUB:TEXT hi", kind="room")
        self.serve()
        self.assertEqual(self.w.result("task-room"), "hello from agy-stub\n", "never in a room with other people")
        self.dm("task-collab", "STUB:TEXT hi", tier="team", collaborator=True)
        self.serve()
        self.assertNotIn("Switched to the", self.w.result("task-collab"))
        self.dm("task-dm", "STUB:TEXT hi")
        self.serve()
        self.assertIn("Switched to the b…@example.com account", self.w.result("task-dm"), "the DM is told once it is back there")
        self.assertEqual(self.posted("$x"), [], "and no card goes anywhere")

    # ── A4 ──
    def outbox(self):
        folder = Path(str(self.w.root) + ".bridge") / "state" / "agent-cards" / "outbox"
        return [json.loads(p.read_text()) for p in sorted(folder.iterdir())] if folder.is_dir() else []

    def test_a_quota_error_queues_one_account_card_to_the_owner_dm_and_a_second_limit_tick_nothing(self):
        self.dm("task-q1", "STUB:QUOTA hi")
        self.serve()
        (queued,) = self.outbox()
        card = queued["card"]
        self.assertEqual((queued["room"], card["id"], card["state"]), (DM, account_switch.CARD, "pending"))
        self.assertEqual(card["account_controls"], {"runtime": "antigravity", "agent": AGENT})
        self.assertEqual(card["text"], "Antigravity has reached its usage limit (it does not say when that resets), so "
                                       "I can't answer until then. Switch to another account or an API key to go on "
                                       "now. I'm using this computer's Google sign-in. Switch this agent's account or "
                                       "API key below.")
        self.assertEqual([b["label"] for b in card["buttons"]], ["Switch account", "Use API key"])
        self.assertEqual(self.w.result("task-q1"), gc.LIMITED_NO_SEND, "the card is the notice: no error line")
        self.dm("task-q2", "STUB:QUOTA again")
        self.serve()
        self.assertEqual(len(self.outbox()), 1, "a second limit tick queues nothing")
        self.assertEqual(len(self.posted("$q1")), 1)
        self.dm("task-q3", "STUB:LIMIT429 and again", kind="room")
        self.serve()
        self.assertEqual((self.outbox(), self.posted("$q3")), ([], []), "nothing more once the card is posted")
        self.assertEqual(self.w.result("task-q3"), gc.LIMITED_NO_SEND)

    def test_a_turn_that_answers_clears_the_limit_and_a_later_limit_is_a_new_card(self):
        self.dm("task-q1", "STUB:LIMIT429 hi")
        self.serve()
        (first,) = self.posted("$q1")
        self.assertIn("(resets 2h 10m)", first["text"], "the reset time in agy's own words")
        self.dm("task-ok", "STUB:TEXT hi")
        self.serve()
        self.dm("task-q2", "STUB:QUOTA hi")
        self.serve()
        self.assertEqual(len(self.posted("$q2")), 1, "the limit came back: a second card")

    def test_another_error_queues_no_card(self):
        self.dm("task-f", "STUB:FAIL hi")
        self.serve()
        self.assertEqual(self.outbox(), [])
        self.assertIn("internal error", self.w.result("task-f"))
        self.assertNotIn("usage limit", self.w.result("task-f"))

    def test_a_quota_error_with_no_owner_dm_seen_sends_no_card_and_says_the_limit_in_the_room(self):
        self.dm("task-q", "STUB:QUOTA hi", kind="room")
        self.serve()
        self.assertEqual(self.outbox(), [])
        self.assertIn("Antigravity has reached its usage limit", self.w.result("task-q"))

    def test_what_reads_as_agys_quota_error(self):
        yes = (("failed", "", "session/prompt: RESOURCE_EXHAUSTED: Individual quota reached."),
               ("failed", "", "HTTP 429 Too Many Requests"), ("failed", "", "status: 429"),
               ("completed", "RESOURCE_EXHAUSTED: quota exceeded", "end_turn"),
               ("completed", "Error: HTTP 429 Too Many Requests", "end_turn"),
               ("completed", "You have exceeded your current quota.", "end_turn"))
        # An ordinary answer that mentions a limit is the answer: it was replaced by the limit text, with a card.
        no = (("failed", "", "internal error"), ("completed", "Your quota exceeded talk: " + "x" * 500, "end_turn"),
              ("failed", "", "port 4290 refused"), ("completed", "", "end_turn"),
              ("completed", "Done. I added a retry for status 429 in fetch.py.", "end_turn"),
              ("completed", "The API returns RESOURCE_EXHAUSTED when the quota exceeded; I handle it.", "end_turn"))
        self.assertEqual([agy_account.quota_error(*t) for t in yes], [True] * len(yes))
        self.assertEqual([agy_account.quota_error(*t) for t in no], [False] * len(no))


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
        self.assertEqual(gk.clear_key(AGENT, run=run, platform="darwin"), gk.REMOVED)
        for argv in calls:
            self.assertNotIn("-w", argv)
            self.assertEqual(argv[argv.index("-s") + 1], gk.agent_service(AGENT))

    def test_clear_key_reports_each_failure_as_a_failure(self):
        def answers(delete, find=44):
            return lambda argv, **kw: subprocess.CompletedProcess(argv, delete if argv[1].startswith("delete") else find, "", "")

        def boom(argv, **kw):
            raise OSError("no security")
        self.assertEqual(gk.clear_key(AGENT, run=answers(44), platform="darwin"), gk.ABSENT)
        self.assertEqual(gk.clear_key(AGENT, run=answers(1), platform="darwin"), gk.FAILED)
        self.assertEqual(gk.clear_key(AGENT, run=answers(0, find=0), platform="darwin"), gk.FAILED, "still there")
        self.assertEqual(gk.clear_key(AGENT, run=boom, platform="darwin"), gk.FAILED)
        self.assertEqual(gk.clear_key(AGENT, run=answers(0), platform="linux"), gk.FAILED)
        self.assertEqual(gk.clear_key("", run=answers(0), platform="darwin"), gk.FAILED)


if __name__ == "__main__":
    unittest.main()
