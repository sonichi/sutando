#!/usr/bin/env python3
"""Antigravity's account card (adapters/antigravity/agy_account.py): the owner, in their DM, sees whether agy
runs on the Google sign-in or on the Gemini API key saved in the app, and switches from there.

Driven through the connector against tests/agy_stub_acp.py, with the Keychain and the Terminal opener faked
and dummy keys only.

Run:
    python3 -m pytest -q packages/ag2-sparrow/tests/test_antigravity_account_card.py
"""
import contextlib
import io
import json
import os
import stat
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

DUMMY_KEY = "AIza" + "D" * 35          # shaped like a Google API key; not a real one
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
        self.cleared = []
        self.opened = []
        patches = [
            mock.patch.object(gk, "key_saved", lambda agent_id, **kw: agent_id == AGENT and self.keychain["saved"]),
            mock.patch.object(gk, "clear_key", self.fake_clear),
            mock.patch.object(gk, "gemini_api_key_from_app", lambda agent_id="", **kw: ""),
            mock.patch.object(agy_account, "open_terminal", lambda argv, state, env=None: self.opened.append(argv) or True),
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
        """One task, with what the connector logs kept."""
        a = self.w.args("--agent-id", AGENT)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            gc.Connector(a).serve()
        return err.getvalue()

    def dm(self, tid, body, reply_to="", click="", tier="owner", collaborator=False):
        lines = ["id: " + tid, "timestamp: 2026-11-22T00:00:00Z", "source: ag2space", "channel_id: " + DM,
                 "channel_kind: dm", "source_message_id: $m-" + tid, "user_id: @alice:s.space",
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

    def menu(self, event="$card1"):
        self.dm("task-ask", "/account")
        self.serve()
        self.assertEqual(self.w.result("task-ask"), account_switch.ANSWERED)
        (card,) = self.posted(event)
        return card

    def tap(self, tid, card, label, event="$card1", **kw):
        said = next(b["reply"] for b in card["buttons"] if b["label"] == label)
        self.dm(tid, said, reply_to=event, click=said, **kw)
        return self.serve()

    def test_the_card_names_the_google_sign_in_and_offers_google_or_a_key(self):
        card = self.menu()
        self.assertEqual([b["label"] for b in card["buttons"]], ["Sign in with Google again", "Use a Gemini API key"])
        self.assertIn("Google sign-in.", card["text"])
        self.assertNotIn("session/prompt", self.w.methods(), "/account never reaches agy")

    def test_with_a_saved_key_the_card_says_so_and_stop_removes_only_this_agents_item(self):
        self.keychain["saved"] = True
        card = self.menu()
        self.assertTrue(card["text"].startswith("Gemini API key."))
        self.assertEqual([b["label"] for b in card["buttons"]], ["Sign in with Google again", "Stop using the key"])
        self.tap("task-stop", card, "Stop using the key")
        self.assertEqual(self.cleared, [AGENT])
        (done,) = self.posted("$card2")
        self.assertEqual(done["state"], "done")
        self.assertIn("fresh session on the Google sign-in", done["text"])
        self.assertEqual(self.w.metrics()[-1]["card"], "account-key-cleared")

    def test_sign_in_with_google_opens_agy_acp_login_on_this_computer_and_says_it_moves_every_agent(self):
        card = self.menu()
        self.tap("task-google", card, "Sign in with Google again")
        self.assertEqual(self.opened, [self.w.args().acp_argv + ["--login"]])
        (info,) = self.posted("$card2")
        self.assertIn("Terminal", info["text"])
        self.assertIn("not here", info["text"])
        self.assertIn(agy_account.SHARED, info["text"])
        self.assertNotIn("session/prompt", self.w.methods())

    def test_use_a_key_points_to_the_connections_card_and_never_asks_for_the_key_here(self):
        card = self.menu()
        self.tap("task-key", card, "Use a Gemini API key")
        (info,) = self.posted("$card2")
        self.assertIn("Connections", info["text"])
        self.assertIn(agy_account.NO_KEYS_HERE, info["text"])
        self.assertFalse(info.get("buttons"))

    def test_a_tap_that_is_not_the_owners_changes_nothing(self):
        self.keychain["saved"] = True
        card = self.menu()
        self.tap("task-team", card, "Stop using the key", tier="team", collaborator=True)
        self.assertIn("couldn't match that button", self.w.result("task-team"))
        self.tap("task-guest", card, "Sign in with Google again", tier="guest")
        self.assertEqual((self.cleared, self.opened, self.posted("$x")), ([], [], []))
        self.assertTrue(self.keychain["saved"])

    def test_a_key_pasted_in_chat_is_never_passed_to_agy_nor_logged(self):
        self.keychain["saved"] = False
        self.dm("task-paste", "here is my key " + DUMMY_KEY)
        logged = self.serve()
        self.assertNotIn("session/prompt", self.w.methods(), "the message never reached agy")
        said = self.w.result("task-paste")
        self.assertIn("did not pass your message on", said)
        records = json.dumps(self.w.records()) + json.dumps(self.w.metrics())
        for where in (logged, said, records):
            self.assertNotIn(DUMMY_KEY, where)
        self.assertEqual(self.w.metrics()[-1]["detail"], "api-key-in-chat")

    def test_ordinary_owner_turns_still_reach_agy_and_are_told_about_the_card(self):
        self.dm("task-hi", "STUB:TEXT hi")
        self.serve()
        self.assertEqual(self.w.result("task-hi"), "hello from agy-stub\n")
        prompt = json.dumps([r for r in self.w.records() if r.get("method") == "session/prompt"])
        self.assertIn(json.dumps(account_switch.MARK)[1:-1], prompt)


class KeychainAndTerminalTest(unittest.TestCase):
    def test_key_saved_asks_for_attributes_only_and_clear_names_this_agents_item(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0 if argv[1] == "delete-generic-password" else 44, "", "")
        self.assertFalse(gk.key_saved(AGENT, run=run, platform="darwin", environ={}))
        self.assertTrue(gk.clear_key(AGENT, run=run, platform="darwin"))
        for argv in calls:
            self.assertNotIn("-w", argv)
            self.assertNotIn("-g", argv)
            self.assertEqual(argv[argv.index("-s") + 1], gk.agent_service(AGENT))
        self.assertEqual([a[1] for a in calls], ["find-generic-password", "delete-generic-password",
                                                 "find-generic-password"])
        self.assertFalse(gk.key_saved("", run=run, platform="darwin"))
        self.assertFalse(gk.key_saved(AGENT, run=run, platform="linux"))

    def test_open_terminal_runs_only_the_quoted_login_command_from_a_private_script(self):
        with tempfile.TemporaryDirectory() as d:
            prog = os.path.join(d, "agy")
            Path(prog).write_text("#!/bin/sh\n")
            os.chmod(prog, 0o700)
            ran = []
            ok = agy_account.open_terminal(["agy", "acp", "--login"], d, env={"PATH": d}, platform="darwin",
                                           run=lambda argv, **kw: ran.append(argv) or subprocess.CompletedProcess(argv, 0))
            script = os.path.join(d, agy_account.SCRIPT_NAME)
            self.assertTrue(ok)
            self.assertEqual(ran, [["/usr/bin/open", "-a", "Terminal", script]])
            self.assertEqual(Path(script).read_text(), "#!/bin/sh\nexec %s acp --login\n" % prog)
            self.assertEqual(stat.S_IMODE(os.stat(script).st_mode), 0o700)
            self.assertFalse(agy_account.open_terminal(["agy", "acp", "--login"], d, env={"PATH": d}, platform="linux",
                                                       run=lambda argv, **kw: ran.append(argv)))
            self.assertFalse(agy_account.open_terminal(["nope", "acp"], d, env={"PATH": d}, platform="darwin",
                                                       run=lambda argv, **kw: ran.append(argv)))
            self.assertEqual(len(ran), 1)

    def test_only_key_shaped_text_is_held_back(self):
        self.assertTrue(agy_account.looks_like_key("x " + DUMMY_KEY + " y"))
        self.assertFalse(agy_account.looks_like_key("switch to the api key please"))


if __name__ == "__main__":
    unittest.main()
