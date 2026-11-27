#!/usr/bin/env python3
"""claude-local's account card: "Use <address>" for each account the app saved, a tap's 0600 switch request,
and the app's answer (or silence) shown in place of the card. Stub CLI and a fake app only."""
from __future__ import annotations

import json
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_claude_code_account_card import Base, DM  # noqa: E402
from claude_code_fixtures import cli  # noqa: E402

from ag2_sparrow import connector_cards  # noqa: E402
from ag2_sparrow.adapters.claude_code import account as accounts, account_card as ac  # noqa: E402

AGENT = "@bot:s.space"
A, B, SARAH = "ann@example.com", "bob@example.com", "sarah@example.com"
CARD = ac.account_switch.CARD


class Remote(Base):
    def setUp(self):
        super().setUp()
        self.appdata = self.home / "appdata"
        self.config = self.appdata / "sparrow" / "agents" / ac.agent_slug(AGENT) / "claude-config"
        self.config.mkdir(parents=True)
        self.configure(BRIDGE_STATE_DIR=str(self.bridge), ALLOW_COLLABORATOR="1", CLAUDE_CONFIG_DIR=str(self.config),
                       AGENT_ID=AGENT)
        self.index(self.appdata, {ac.saved_key(A): A + " (max)", ac.saved_key(SARAH): SARAH, ac.saved_key(B): B,
                                  "notakey": "c@example.com", ac.saved_key("m"): "s…@example.com", ac.saved_key("x"): 7})
        self.answer = {"loggedIn": True, "email": SARAH, "authMethod": "claude.ai"}
        p = mock.patch.object(ac, "_run", lambda argv, **kw: mock.Mock(stdout=json.dumps(self.answer)))
        p.start()
        self.addCleanup(p.stop)
        self.spawned = []
        p = mock.patch.object(ac, "_popen", lambda argv, **kw: self.spawned.append(argv))
        p.start()
        self.addCleanup(p.stop)

    def index(self, appdata, data):
        path = Path(appdata).joinpath(*ac.STORE) / ac.saved_key(AGENT) / ac.INDEX
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return path

    def cfg(self):
        return cli.read_config(cli.config_path())

    def posted(self, event="$acct"):
        """/account in the DM, its card posted and recorded as the bridge and the connector do."""
        self.asks = getattr(self, "asks", 0) + 1
        rc, out = self.claim(self.task("task-a%d" % self.asks, "/account"))
        self.assertIn("(account-card)", out["refused"])
        card = self.queued()[-1]["card"]
        _, issued = connector_cards._core()
        issued.record(Path(self.root), event, card, room=DM, lane="")
        for p in self.bridge.joinpath(*connector_cards.OUTBOX).glob("*.json"):
            p.unlink()
        return card

    def tap(self, task_id, words, event="$acct", card_id=CARD, **kw):
        return self.claim(self.task(task_id, words, reply_to=event, click={"card_id": card_id, "action": words}, **kw))

    def request(self):
        return self.config / ac.REQUEST


class Offer(Remote):
    def test_the_card_offers_each_other_saved_account_by_address_sorted_and_not_the_one_in_use(self):
        card = self.posted()
        self.assertEqual([b["label"] for b in card["buttons"]], ["Use " + A, "Use " + B, "Switch account"])
        self.assertEqual(card["buttons"][0]["command"], "card:account:agent:" + ac.saved_key(A))
        self.assertEqual(card["buttons"][0]["reply"], "Use " + A, "the plan suffix is never shown")

    def test_at_most_five_accounts_are_offered_and_all_when_the_account_in_use_is_unknown(self):
        many = {ac.saved_key("u%d@example.com" % i): "u%d@example.com" % i for i in range(8)}
        self.index(self.appdata, many)
        self.answer = {"loggedIn": False}
        card = self.posted()
        self.assertEqual([b["label"] for b in card["buttons"]][:-1], ["Use u%d@example.com" % i for i in range(5)])

    def test_no_buttons_when_a_switch_here_would_be_refused(self):
        for over in ({"CLAUDE_CONFIG_DIR": ""}, {"CLAUDE_CONFIG_DIR": str(self.home / ".claude-sutando")}):
            self.configure(BRIDGE_STATE_DIR=str(self.bridge), AGENT_ID=AGENT, **over)
            card = self.posted()
            self.assertEqual([b["label"] for b in card["buttons"]], ["Switch account"], over)
        with mock.patch.dict(ac.os.environ, {accounts.PROXY_VAR: "http://localhost:7846"}):
            self.configure(BRIDGE_STATE_DIR=str(self.bridge), AGENT_ID=AGENT, CLAUDE_CONFIG_DIR=str(self.config))
            self.assertEqual([b["label"] for b in self.posted()["buttons"]], ["Switch account"])

    def test_where_the_app_keeps_its_data(self):
        cfg = {"AGENT_ID": AGENT, "CLAUDE_CONFIG_DIR": str(self.config)}
        home = self.home
        with mock.patch.object(ac.sys, "platform", "darwin"):
            dirs = ac.app_data_dirs(cfg, home=home)
        self.assertEqual(dirs, [self.appdata, home / "Library" / "Application Support" / "space.ag2.desktop",
                                home / "Library" / "Application Support" / "space.ag2.app"])
        with mock.patch.object(ac.sys, "platform", "linux"):
            self.assertEqual(ac.app_data_dirs({}, home=home, environ={"XDG_DATA_HOME": "/x"}), [Path("/x/space.ag2.app")])
            self.assertEqual(ac.app_data_dirs({"AGENT_ID": "@other:s.space", "CLAUDE_CONFIG_DIR": str(self.config)},
                                              home=home, environ={}), [home / ".local" / "share" / "space.ag2.app"])
        with mock.patch.object(ac.sys, "platform", "win32"):
            self.assertEqual(ac.app_data_dirs({}, home=home, environ={"APPDATA": "/w"}), [Path("/w/space.ag2.app")])
            self.assertEqual(ac.app_data_dirs({}, home=home, environ={}), [])
        self.assertEqual(ac.agent_slug("@liu-x.agent:s.space"), "liu-x.agent_s.space-" + ac.hashlib.sha256(
            b"@liu-x.agent:s.space").hexdigest()[:12])

    def test_the_index_is_read_carefully(self):
        cfg = {"AGENT_ID": AGENT}
        self.assertEqual(ac.saved_accounts({}, dirs=[self.appdata]), {})
        self.assertEqual(set(ac.saved_accounts(cfg, dirs=[self.home / "none", self.appdata]).values()), {A, B, SARAH})
        self.index(self.appdata, "not json")
        self.assertEqual(ac.saved_accounts(cfg, dirs=[self.appdata]), {})
        self.index(self.appdata, "[1]")
        self.assertEqual(ac.saved_accounts(cfg, dirs=[self.appdata]), {})
        self.assertEqual(ac.saved_accounts(cfg, dirs=[self.home / "none"]), {})
        self.assertEqual(ac.email_only("a@b.c (team (x))"), "a@b.c (team (x))")


class Tap(Remote):
    def test_a_tap_writes_the_apps_request_owner_only_and_shows_switching_in_place(self):
        self.posted()
        rc, out = self.tap("task-u", "Use " + A)
        self.assertEqual(rc, 1)
        self.assertIn("(account-switching)", out["refused"])
        self.assertEqual(stat.S_IMODE(self.request().stat().st_mode), 0o600)
        req = json.loads(self.request().read_text())
        self.assertEqual(sorted(req), ["at", "key", "nonce", "v"])
        self.assertEqual((req["v"], req["key"]), (1, ac.saved_key(A)))
        self.assertTrue(ac.NONCE_RE.fullmatch(req["nonce"]))
        self.assertIsInstance(req["at"], int)
        self.assertEqual([p.name for p in self.config.iterdir()], [ac.REQUEST], "no temp file left")
        queued, = self.queued()
        self.assertEqual((queued["edit"], queued["card"]["text"], queued["card"]["id"]),
                         ("$acct", "Switching to %s…" % A, ac.account_switch.NOTE))
        argv, = self.spawned
        self.assertEqual(argv[-5:], ["await", "--root", self.root, "--nonce", req["nonce"]])
        pending = Path(self.root) / "state" / ac.PENDING / (req["nonce"] + ".json")
        self.assertEqual(stat.S_IMODE(pending.stat().st_mode), 0o600)

    def refused(self, rc, task_id):
        self.assertEqual(rc, 1)
        self.assertFalse(self.request().exists(), "no request written")
        self.assertEqual(self.spawned, [])
        return (self.results / (task_id + ".txt")).read_text()

    def test_a_key_not_offered_a_non_owner_a_room_and_another_card_are_refused(self):
        self.posted()
        self.posted("$menu")
        rc, _ = self.tap("task-1", "Use " + SARAH)
        self.refused(rc, "task-1")      # not on that card: the card is shown afresh in its place (`expired`)
        rc, _ = self.tap("task-2", "Use " + A, tier="team", extra="collaborator: true\n")
        self.assertIn("couldn't match", self.refused(rc, "task-2"))
        rc, _ = self.tap("task-3", "Use " + A, sender="@eve:s.space", tier="team")
        self.refused(rc, "task-3")
        rc, _ = self.tap("task-4", "Use " + A, kind="room")
        self.refused(rc, "task-4")
        rc, _ = self.tap("task-5", "Use " + A, event="$elsewhere")
        self.refused(rc, "task-5")
        rc, _ = self.tap("task-6", "Use " + A, card_id="model-switch")
        self.refused(rc, "task-6")
        card = ac.AccountCard(self.root, self.cfg(), lambda path, body: self.said.append(body), print)
        self.said = []
        sent = {"channel_id": DM, "reply_to_event": "$nothere"}
        self.assertEqual(card.tapped(ac.saved_key(A), sent, "x"), "account-unknown")
        self.assertEqual(card.tapped(ac.saved_key(SARAH), dict(sent, reply_to_event="$acct"), "x"), "account-unknown")
        self.assertEqual(card.tapped(ac.saved_key(A), {"channel_id": DM}, "x"), "account-unknown")
        self.assertIn(ac.UNMATCHED, self.said[0])
        self.assertFalse(self.request().exists())

    def use_directly(self, **over):
        self.configure(BRIDGE_STATE_DIR=str(self.bridge), AGENT_ID=AGENT, **over)
        cfg = self.cfg()
        card = ac.AccountCard(self.root, cfg, lambda *a: None, print)
        _, issued = connector_cards._core()
        built = card.card("pending", "x", ask=True)[1]
        built["buttons"].insert(0, {"label": "Use " + A, "kind": "reply", "reply": "Use " + A,
                                    "command": "card:account:agent:" + ac.saved_key(A)})
        issued.record(Path(self.root), "$own", built, room=DM, lane="")
        return card.tapped(ac.saved_key(A), {"channel_id": DM, "reply_to_event": "$own"}, None)

    def test_no_config_dir_of_its_own_or_the_cores_is_refused_in_one_line(self):
        fallback = ac.app_data_dirs({})[0]
        self.index(fallback, {ac.saved_key(A): A})
        for over, line in (({"CLAUDE_CONFIG_DIR": ""}, accounts.SHARED),
                           ({"CLAUDE_CONFIG_DIR": str(self.home / ".claude-sutando")}, accounts.CORES)):
            self.assertEqual(self.use_directly(**over), "account-refused")
            note = self.queued()[-1]
            self.assertEqual((note["edit"], note["card"]["text"]), ("$own", "Could not switch: " + line))
        self.assertFalse(self.request().exists())
        with mock.patch.dict(ac.os.environ, {accounts.PROXY_VAR: "http://localhost:7846"}):
            self.assertEqual(self.use_directly(CLAUDE_CONFIG_DIR=str(self.config)), "account-refused")
        self.assertIn(accounts.PROXIED, self.queued()[-1]["card"]["text"])
        with mock.patch("ag2_sparrow.adapters.claude_code.cli.other_agents_dirs", side_effect=RuntimeError("x")):
            self.assertEqual(self.use_directly(CLAUDE_CONFIG_DIR=str(self.config)), "account-switching")

    def test_an_account_no_longer_saved_and_a_request_that_cannot_be_written(self):
        self.index(self.appdata, {})
        self.assertEqual(self.use_directly(CLAUDE_CONFIG_DIR=str(self.config)), "account-not-saved")
        self.assertEqual(self.queued()[-1]["card"]["text"], ac.NOT_SAVED)
        self.index(self.appdata, {ac.saved_key(A): A})
        with mock.patch.object(ac, "write_request", side_effect=OSError("read-only")):
            self.assertEqual(self.use_directly(CLAUDE_CONFIG_DIR=str(self.config)), "account-not-asked")
        self.assertIn("could not be written", self.queued()[-1]["card"]["text"])
        with mock.patch.object(ac.os, "replace", side_effect=OSError("x")), self.assertRaises(OSError):
            ac.write_request(self.config, ac.saved_key(A))
        self.assertEqual(list(self.config.iterdir()), [], "the half-written request is removed")

    def test_a_watcher_that_cannot_start_is_said(self):
        with mock.patch.object(ac, "_popen", side_effect=OSError("no fork")):
            self.assertEqual(self.use_directly(CLAUDE_CONFIG_DIR=str(self.config)), "account-switching")
        self.assertIn("won't confirm here", self.queued()[-1]["card"]["text"])


class Answer(Remote):
    def begun(self):
        self.posted()
        self.taps = getattr(self, "taps", 0) + 1
        self.tap("task-u%d" % self.taps, "Use " + A)
        for p in self.bridge.joinpath(*connector_cards.OUTBOX).glob("*.json"):
            p.unlink()
        return json.loads(self.request().read_text())["nonce"]

    def result(self, **data):
        (self.config / ac.RESULT).write_text(json.dumps(dict({"v": 1}, **data)))

    def test_switched_and_could_not_switch_are_shown_in_place_and_the_answer_taken(self):
        nonce = self.begun()
        self.result(nonce=nonce, ok=True, label=A + " (max)", error=None)
        self.assertEqual(ac.await_answer(self.root, nonce, sleep=lambda s: None), "Switched to %s." % A)
        self.assertFalse((self.config / ac.RESULT).exists())
        queued, = self.queued()
        self.assertEqual((queued["edit"], queued["card"]["state"]), ("$acct", "done"))
        self.assertFalse((Path(self.root) / "state" / ac.PENDING / (nonce + ".json")).exists())
        self.assertEqual(ac.await_answer(self.root, nonce), "", "told once")
        nonce = self.begun()
        self.result(nonce=nonce, ok=False, error="the saved sign-in\nhas expired")
        self.assertEqual(ac.await_answer(self.root, nonce), "Could not switch: the saved sign-in has expired")
        self.assertEqual(ac.told({"ok": False}, A), ("error", "Could not switch: the app gave no reason"))
        self.assertEqual(ac.told({"ok": True}, A), ("done", "Switched to %s." % A))

    def test_no_answer_in_90_seconds_is_said_and_the_request_withdrawn(self):
        nonce = self.begun()
        self.result(nonce="00000000-0000-4000-8000-000000000000", ok=True, label=B)
        now = [0.0]

        def sleep(s):
            now[0] += s
        words = ac.await_answer(self.root, nonce, sleep=sleep, clock=lambda: now[0], log=print)
        self.assertEqual(words, "Could not switch: " + ac.NO_ANSWER)
        self.assertGreaterEqual(now[0], ac.AWAIT_S)
        self.assertFalse(self.request().exists(), "a late app does not switch unseen")
        self.assertTrue((self.config / ac.RESULT).exists(), "another request's answer is left for its watcher")
        self.assertEqual(self.queued()[-1]["card"]["text"], "Could not switch: " + ac.NO_ANSWER)

    def test_odd_files_and_nonces_are_not_answers(self):
        nonce = self.begun()
        self.assertIsNone(ac.answer(self.config, nonce), "no answer yet")
        (self.config / ac.RESULT).write_text("not json")
        self.assertIsNone(ac.answer(self.config, nonce))
        (self.config / ac.RESULT).write_text("[]")
        self.assertIsNone(ac.answer(self.config, nonce))
        self.request().write_text("not json")
        self.assertEqual(ac.await_answer(self.root, nonce, sleep=lambda s: None, clock=iter([0, 0, 999]).__next__),
                         "Could not switch: " + ac.NO_ANSWER)
        self.assertTrue(self.request().exists(), "a request that is not this one's is left")
        self.assertEqual(ac.await_answer(self.root, "../x"), "")
        bad = Path(self.root) / "state" / ac.PENDING / (nonce + ".json")
        bad.write_text("not json")
        self.assertEqual(ac.await_answer(self.root, nonce), "")
        with mock.patch.object(ac.card_desk, "post", return_value=False):
            nonce = self.begun()
            self.result(nonce=nonce, ok=True, label=A)
            self.assertEqual(ac.await_answer(self.root, nonce, log=lambda *a: None), "Switched to %s." % A)

    def test_the_detached_step_runs_by_its_command(self):
        nonce = self.begun()
        self.result(nonce=nonce, ok=True, label=A)
        with mock.patch("builtins.print") as said:
            self.assertEqual(ac.main(["await", "--root", self.root, "--nonce", nonce]), 0)
            self.assertEqual(ac.main(["await", "--root", self.root, "--nonce", nonce]), 0)
        self.assertEqual([c.args[0] for c in said.call_args_list], ["Switched to %s." % A, "no such request"])


if __name__ == "__main__":
    unittest.main()
