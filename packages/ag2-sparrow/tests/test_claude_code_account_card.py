#!/usr/bin/env python3
"""claude-local's account card: the owner's /account or /login in their DM and their tap on the card are
answered by `inbox claim` (never submitted to the session); nothing signs in here and no code travels in
chat. Stub CLI only: no real Claude runs."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_code_fixtures import cli, ConnectBase, hold_consumer, inbox, unhold  # noqa: E402
from ag2_sparrow import connector_cards, model_switch  # noqa: E402

from ag2_sparrow.adapters.claude_code import account_card, model_card  # noqa: E402

OWNER, DM = "@owner:s.space", "!dm:s.space"
# The address as the shared account card shows one (account_switch.masked): this suite does not fix that rule.
SHOWN = account_card.account_switch.masked("sarah@example.com")
REAL_START_APPLY = model_card.start_apply


class Base(ConnectBase):
    def setUp(self):
        super().setUp()
        self.bridge = self.home / "bridge-state"
        self.bridge.mkdir()
        self.configure(BRIDGE_STATE_DIR=str(self.bridge), ALLOW_COLLABORATOR="1")
        self.assertEqual(inbox.prepare_root(str(self.work)), "")
        self.holder, _ = hold_consumer(self.work)
        self.addCleanup(unhold, self.holder)
        self.root = str(self.work)
        self.started = []
        p = mock.patch.object(model_card, "start_apply", lambda root, popen=None, environ=None: self.started.append(root))
        p.start()
        self.addCleanup(p.stop)

    def task(self, task_id, said, tier="owner", kind="dm", click=None, reply_to="", sender=OWNER, extra=""):
        head = ("access_tier: %s\nuser_id: %s\nchannel_id: %s\nchannel_kind: %s\n%s" % (tier, sender, DM, kind, extra)
                + ("reply_to_event: %s\n" % reply_to if reply_to else "")
                + ("card_click: %s\n" % json.dumps(click) if click else ""))
        (self.inbox / (task_id + ".txt")).write_text("id: %s\n%stask: %s\nsource: ag2space\n" % (task_id, head, said))
        return task_id

    def claim(self, task_id):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["inbox", "--root", self.root, "claim", task_id])
        return rc, json.loads(out.getvalue())

    def queued(self):
        return [json.loads(p.read_text()) for p in sorted(self.bridge.joinpath(*connector_cards.OUTBOX).glob("*.json"))]

    def menu_posted(self):
        """The menu, as the bridge posts it and the connector records it: its buttons then act."""
        cfg = cli.read_config(cli.config_path())
        body, card = model_card.switcher(self.root, cfg).card(model_card.switcher(self.root, cfg).state())
        _, issued = connector_cards._core()
        issued.record(Path(self.root), "$menu", card, room=DM, lane="")
        return card

    def state(self):
        return json.loads((Path(self.root) / "state" / model_switch.FILE).read_text())


class Account(Base):
    """`/account` or `/login` in the owner's DM, or a tap on the account card: the account (masked) and where
    to switch it, answered in the claim. Nothing signs in here, and no code ever travels in chat."""

    def setUp(self):
        super().setUp()
        self.answer = {"loggedIn": True, "email": "sarah@example.com", "subscriptionType": "max", "authMethod": "claude.ai"}
        self.runs = []

        def run(argv, **kw):
            self.runs.append((argv, kw.get("env", {}).get("CLAUDE_CONFIG_DIR")))
            if isinstance(self.answer, Exception):
                raise self.answer
            return mock.Mock(stdout=json.dumps(self.answer) if isinstance(self.answer, dict) else str(self.answer))
        p = mock.patch.object(account_card, "_run", run)
        p.start()
        self.addCleanup(p.stop)

    def account_menu(self):
        cfg = cli.read_config(cli.config_path())
        card = account_card.AccountCard(self.root, cfg, lambda *a: None, print).card("pending", "x", ask=True)[1]
        _, issued = connector_cards._core()
        issued.record(Path(self.root), "$acct", card, room=DM, lane="")

    def test_account_and_login_in_the_dm_show_the_masked_account_and_where_to_switch(self):
        for t, said in (("task-a", "/account"), ("task-l", "/login")):
            rc, out = self.claim(self.task(t, said))
            self.assertEqual(rc, 1, t)
            self.assertIn("answered by its card (account-card)", out["refused"])
            self.assertNotIn("admission", out, "never submitted to the session")
        cards = [q["card"] for q in self.queued()]
        self.assertEqual(len(cards), 2)
        text = cards[0]["text"]
        self.assertIn(SHOWN + ".", text)
        self.assertTrue(text.endswith(account_card.account_switch.BELOW), text)
        self.assertNotIn("Settings", text, "never sent elsewhere")
        self.assertEqual(cards[0]["account_controls"], {"runtime": "claude"})
        self.assertFalse(re.search(r"[\u4e00-\u9fff]", json.dumps(cards, ensure_ascii=False)), "English only")
        self.assertNotIn("(max)", json.dumps(cards), "the full email (owner 2026-11-24), never a plan")
        self.assertEqual([b["label"] for b in cards[0]["buttons"]], ["Switch account"])
        argv, config = self.runs[0]
        self.assertEqual(argv[-3:], ["auth", "status", "--json"])
        self.assertEqual(config, cli.read_config(cli.config_path()).get("CLAUDE_CONFIG_DIR") or None)

    def test_which_account_asked_in_the_owners_own_words_is_the_card_and_never_reaches_the_session(self):
        rc, out = self.claim(self.task("task-w", "which account are you on?"))
        self.assertEqual(rc, 1)
        self.assertIn("answered by its card (account-card)", out["refused"])
        card, = [q["card"] for q in self.queued()]
        self.assertIn(SHOWN + ".", card["text"])
        rc, out = self.claim(self.task("task-g", "create an account on GitHub for the demo and tell me which one"))
        self.assertEqual((rc, len(self.queued())), (0, 1), "a longer message about some other account is the session's")

    def test_switch_account_points_to_the_app_and_starts_nothing(self):
        self.account_menu()
        said = "Switch account"
        rc, out = self.claim(self.task("task-s", said, reply_to="$acct",
                                       click={"card_id": account_card.account_switch.CARD, "action": said}))
        self.assertEqual(rc, 1)
        self.assertIn("(account-where)", out["refused"])
        queued, = self.queued()
        note = queued["card"]
        self.assertEqual(queued.get("edit"), "$acct", "shown in place of the card tapped, not as a second card")
        self.assertEqual(note["text"], "Open this on the computer that runs Claude Code to switch account. The sign-in "
                                       "code goes into the card there, never into this chat.")
        # Opened there, the same card draws the controls; it is not the menu, so it retires no account card.
        self.assertEqual((note["account_controls"], note["id"]), ({"runtime": "claude"}, account_card.account_switch.NOTE))
        self.assertNotIn("buttons", note)
        self.assertEqual(self.runs, [], "no sign-in, no status call")

    def test_a_tap_by_a_collaborator_is_refused_and_their_account_ask_goes_to_the_session(self):
        self.account_menu()
        said = "Switch account"
        rc, out = self.claim(self.task("task-c", said, reply_to="$acct", tier="team", extra="collaborator: true\n",
                                       click={"card_id": account_card.account_switch.CARD, "action": said}))
        self.assertEqual(rc, 1)
        self.assertIn("couldn't match that button", (self.results / "task-c.txt").read_text())
        rc, out = self.claim(self.task("task-r", "/account", kind="room"))
        self.assertEqual((rc, out.get("admission")), (0, "owner"), "outside the DM it is ordinary text")

    def test_signed_out_unreadable_and_proxied_are_said_as_such(self):
        cfg = cli.read_config(cli.config_path())
        self.answer = {"loggedIn": False}
        self.assertEqual(account_card.label(cfg), "")
        self.answer = "not json"
        with self.assertRaises(RuntimeError):
            account_card.label(cfg)
        self.answer = OSError("no claude")
        self.assertIsNone(account_card.status(cfg)["signed_in"])
        self.answer = {"loggedIn": True}
        self.assertEqual(account_card.label(cfg), "an Anthropic account")
        self.assertIn("credential proxy", account_card.label(cfg, env={"ANTHROPIC_BASE_URL": "http://localhost:7846"}))
        self.answer = {"loggedIn": True, "email": "sarah@example.com"}
        self.assertEqual(account_card.label(cfg), SHOWN)
        for plan in ("max", "pro", "team"):  # owner 2026-11-24: an account card never shows a plan suffix
            self.answer = {"loggedIn": True, "email": "sarah@example.com", "subscriptionType": plan}
            self.assertEqual(account_card.label(cfg), SHOWN, plan)
        self.answer = {"loggedIn": True, "subscriptionType": "max"}
        self.assertEqual(account_card.label(cfg), "an Anthropic account")
        card = account_card.AccountCard(self.root, cfg, lambda *a: None, print)
        self.answer = {"loggedIn": False}
        self.assertEqual(card.using(), "I'm not signed in.")
        self.answer = "nope"
        self.assertEqual(card.using(), "I could not read which account I'm signed in with.")
        out = []
        self.assertEqual(card.tapped("other", {}, str(self.results / "x.txt")), "account-unknown")

    def test_the_account_mark_shows_the_account_card(self):
        t = self.task("task-q", "can you switch account?")
        self.assertTrue(inbox.marked(self.root, t, b"  [account-card]  ").startswith(b"[no-send]"))
        card, = [q["card"] for q in self.queued()]
        self.assertIn(SHOWN + ".", card["text"])


if __name__ == "__main__":
    unittest.main()
