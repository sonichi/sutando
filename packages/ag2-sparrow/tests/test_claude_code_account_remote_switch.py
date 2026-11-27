#!/usr/bin/env python3
"""claude-local's account card: "Use <address>" for each account the app saved, a tap's 0600 switch request,
and the app's answer (or silence) shown in place of the card. Stub CLI and a fake app only."""
from __future__ import annotations

import json
import os
import stat
import sys
import time
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
from ag2_sparrow.adapters.claude_code import account as accounts, account_card as ac, account_remote as rm  # noqa: E402

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
        p = mock.patch.object(rm, "_popen", lambda argv, **kw: self.spawned.append(argv))
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
        return self.config / rm.REQUEST

    def nonce(self):
        return json.loads(self.request().read_text())["nonce"]

    def claim_it(self, nonce):
        """The app takes the request: renamed to its in-progress marker."""
        self.request().rename(self.config / (rm.CLAIMED % nonce))

    def result(self, nonce, **data):
        """The app's receipt; a success is a verified one unless the test says otherwise, as the app sends it."""
        data = dict({"verified": True} if data.get("ok") is True else {}, **data)
        (self.config / (rm.RESULT % nonce)).write_text(json.dumps(dict({"v": 2, "nonce": nonce}, **data)))
        (self.config / (rm.CLAIMED % nonce)).unlink(missing_ok=True)

    def clear_outbox(self):
        for p in self.bridge.joinpath(*connector_cards.OUTBOX).glob("*.json"):
            p.unlink()

    def begun(self):
        """A tap on a fresh card that asked the app to switch to A; its nonce."""
        self.posted()
        self.taps = getattr(self, "taps", 0) + 1
        rc, out = self.tap("task-u%d" % self.taps, "Use " + A)
        self.assertIn("(account-switching)", out["refused"])
        self.clear_outbox()
        return self.nonce()

    def texts(self):
        return [(q.get("edit"), q["card"]["text"]) for q in self.queued()]


class Offer(Remote):
    def test_the_card_offers_each_other_saved_account_by_address_sorted_and_not_the_one_in_use(self):
        card = self.posted()
        self.assertEqual([b["label"] for b in card["buttons"]], ["Use " + A, "Use " + B, "Switch account", ac.SIGN_IN_LABEL])
        self.assertEqual(card["buttons"][0]["command"], "card:account:agent:" + ac.saved_key(A))
        self.assertEqual(card["buttons"][0]["reply"], "Use " + A, "the plan suffix is never shown")

    def test_at_most_five_accounts_are_offered_and_all_when_the_account_in_use_is_unknown(self):
        many = {ac.saved_key("u%d@example.com" % i): "u%d@example.com" % i for i in range(8)}
        self.index(self.appdata, many)
        self.answer = {"loggedIn": False}
        card = self.posted()
        # Six buttons at most: "Switch account" and "Sign in to another account" leave room for four.
        self.assertEqual([b["label"] for b in card["buttons"]][:-2], ["Use u%d@example.com" % i for i in range(4)])

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
    def test_a_tap_writes_the_apps_v2_request_owner_only_and_shows_switching_in_place(self):
        self.posted()
        rc, out = self.tap("task-u", "Use " + A)
        self.assertEqual(rc, 1)
        self.assertIn("(account-switching)", out["refused"])
        self.assertEqual(stat.S_IMODE(self.request().stat().st_mode), 0o600)
        req = json.loads(self.request().read_text())
        self.assertEqual(sorted(req), ["at", "key", "nonce", "v"])
        self.assertEqual((req["v"], req["key"]), (2, ac.saved_key(A)))
        self.assertTrue(rm.NONCE_RE.fullmatch(req["nonce"]))
        self.assertIsInstance(req["at"], int)
        self.assertEqual([p.name for p in self.config.iterdir()], [rm.REQUEST], "no temp file left")
        self.assertEqual(self.texts(), [("$acct", "Switching to %s…" % A)])
        self.assertEqual(self.queued()[0]["card"]["id"], ac.account_switch.NOTE)
        argv, = self.spawned
        self.assertEqual(argv[-5:], ["await", "--root", self.root, "--nonce", req["nonce"]])
        rec = rm.folder(self.root) / (req["nonce"] + ".json")
        self.assertEqual(stat.S_IMODE(rec.stat().st_mode), 0o600)
        self.assertEqual({k: rm.load(self.root, req["nonce"])[k] for k in ("label", "room", "edit")},
                         {"label": A, "room": DM, "edit": "$acct"})

    def test_the_switch_records_never_refuse_the_next_start(self):
        from ag2_sparrow import task_lifecycle
        self.begun()
        (Path(self.root) / "state" / "account-remote.log").touch()
        self.assertIsNone(task_lifecycle.prepare_work_dir(self.root, check_only=True))

    def test_a_second_tap_while_one_is_outstanding_is_refused_and_writes_nothing(self):
        first = self.begun()
        self.posted("$second")
        rc, out = self.tap("task-2", "Use " + B, event="$second")
        self.assertIn("(account-busy)", out["refused"])
        self.assertEqual(self.texts(), [("$second", rm.BUSY)])
        self.assertEqual(self.nonce(), first, "the first request is untouched")
        self.assertEqual([r["nonce"] for r in rm.outstanding(self.root)], [first])
        self.claim_it(first)        # claimed by the app: still one outstanding
        self.clear_outbox()
        self.posted("$third")
        rc, out = self.tap("task-3", "Use " + B, event="$third")
        self.assertIn("(account-busy)", out["refused"])
        self.assertFalse(self.request().exists())
        self.assertEqual(len(self.spawned), 1)

    def test_a_request_or_claim_left_by_anything_else_also_blocks(self):
        self.request().write_text("{}")
        self.assertIsNone(rm.publish(self.root, self.config, "k", A, DM, "$x", str(self.bridge)))
        self.request().unlink()
        (self.config / (rm.CLAIMED % "other")).write_text("{}")
        old = time.time() - 10 * rm.STALE_CLAIM_S
        os.utime(self.config / (rm.CLAIMED % "other"), (old, old))
        self.assertIsNone(rm.publish(self.root, self.config, "k", A, DM, "$x", str(self.bridge)),
                          "a claim, however old, is the app's to release")
        self.assertTrue((self.config / (rm.CLAIMED % "other")).exists())
        self.assertEqual(rm.outstanding(self.root), [])

    def test_the_exclusive_create_lets_only_one_writer_win(self):
        rm.write_request(self.config, "a" * 16, "11111111-1111-4111-8111-111111111111", 1)
        with self.assertRaises(FileExistsError):
            rm.write_request(self.config, "b" * 16, "22222222-2222-4222-8222-222222222222", 2)
        self.assertEqual(rm.request_nonce(self.config), "11111111-1111-4111-8111-111111111111")
        self.assertEqual(sorted(p.name for p in self.config.iterdir()), [rm.REQUEST], "the loser leaves no temp file")
        self.request().unlink()
        with mock.patch.object(rm, "write_request", side_effect=FileExistsError("raced")), self.assertRaises(OSError):
            rm.publish(self.root, self.config, "k", A, DM, "$x", str(self.bridge))
        self.assertEqual(rm.outstanding(self.root), [], "a lost race keeps nothing")

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
        said = []
        card = ac.AccountCard(self.root, self.cfg(), lambda path, body: said.append(body), print)
        sent = {"channel_id": DM, "reply_to_event": "$nothere"}
        self.assertEqual(card.tapped(ac.saved_key(A), sent, "x"), "account-unknown")
        self.assertEqual(card.tapped(ac.saved_key(SARAH), dict(sent, reply_to_event="$acct"), "x"), "account-unknown")
        self.assertEqual(card.tapped(ac.saved_key(A), {"channel_id": DM}, "x"), "account-unknown")
        self.assertIn(ac.UNMATCHED, said[0])
        self.assertFalse(self.request().exists())

    def use_directly(self, event="$own", **over):
        self.configure(BRIDGE_STATE_DIR=str(self.bridge), AGENT_ID=AGENT, **over)
        card = ac.AccountCard(self.root, self.cfg(), lambda *a: None, print)
        _, issued = connector_cards._core()
        built = card.card("pending", "x", ask=True)[1]
        built["buttons"].insert(0, {"label": "Use " + A, "kind": "reply", "reply": "Use " + A,
                                    "command": "card:account:agent:" + ac.saved_key(A)})
        issued.record(Path(self.root), event, built, room=DM, lane="")
        return card.tapped(ac.saved_key(A), {"channel_id": DM, "reply_to_event": event}, None)

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
            self.assertEqual(self.use_directly(CLAUDE_CONFIG_DIR=str(self.config)), "account-refused")
            self.assertEqual(self.queued()[-1]["card"]["text"], "Could not switch: " + ac.OTHERS_UNKNOWN)
            self.assertNotIn("Use " + A, [b["label"] for b in self.posted("$x")["buttons"]], "fail closed: no buttons")
        self.assertFalse(self.request().exists())
        from ag2_sparrow.adapters.claude_code import cli as cc
        with mock.patch.object(cc.os, "scandir", side_effect=OSError("x")), self.assertRaises(OSError):
            cc.other_agents_dirs(cc.LOCAL, strict=True)
        with mock.patch.object(Path, "rglob", side_effect=OSError("x")):
            self.assertEqual(cc.other_agents_dirs(cc.LOCAL), [], "the sign-in's own reading is as before")

    def test_a_peer_config_that_cannot_be_read_or_parsed_refuses_the_switch(self):
        from ag2_sparrow.adapters.claude_code import cli as cc
        root = cc.cm.config_root()
        for name, make in (("peer-dir.conf", lambda p: p.mkdir(parents=True)),
                           ("peer-bad.conf", lambda p: p.write_text("CLAUDE_CONFIG_DIR='unterminated\n"))):
            peer = root / "peers" / name
            peer.parent.mkdir(parents=True, exist_ok=True)
            make(peer)
            self.assertEqual([b["label"] for b in self.posted("$p" + name)["buttons"]], ["Switch account"], name)
            self.assertEqual(self.use_directly("$u" + name, CLAUDE_CONFIG_DIR=str(self.config)), "account-refused", name)
            self.assertEqual(self.queued()[-1]["card"]["text"], "Could not switch: " + ac.OTHERS_UNKNOWN)
            self.assertFalse(self.request().exists())
            self.assertEqual(cc.other_agents_dirs(cc.LOCAL), [], "other callers read leniently, as before")
            (peer.rmdir() if peer.is_dir() else peer.unlink())
        (root / "peers" / "peer.conf").write_text("CLAUDE_CONFIG_DIR=%s\n" % (self.home / "elsewhere"))
        self.assertEqual(cc.other_agents_dirs(cc.LOCAL, strict=True), [str(self.home / "elsewhere")])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root lists any directory")
    def test_a_config_root_that_cannot_be_listed_whole_refuses_the_switch(self):
        from ag2_sparrow.adapters.claude_code import cli as cc
        root = cc.cm.config_root()
        (root / "seen").mkdir(parents=True)
        (root / "seen" / "peer.conf").write_text("CLAUDE_CONFIG_DIR=%s\n" % (self.home / "elsewhere"))
        hidden = root / "hidden"
        (hidden / "deeper").mkdir(parents=True)
        (hidden / "deeper" / "peer.conf").write_text("CLAUDE_CONFIG_DIR=%s\n" % self.config)
        hidden.chmod(0)
        self.addCleanup(hidden.chmod, 0o700)
        with self.assertRaises(PermissionError):
            cc.other_agents_dirs(cc.LOCAL, strict=True)
        self.assertEqual(cc.other_agents_dirs(cc.LOCAL), [str(self.home / "elsewhere")], "lenient: what it can")
        self.assertEqual([b["label"] for b in self.posted("$h")["buttons"]], ["Switch account"])
        self.assertEqual(self.use_directly("$hu", CLAUDE_CONFIG_DIR=str(self.config)), "account-refused")
        self.assertEqual(self.queued()[-1]["card"]["text"], "Could not switch: " + ac.OTHERS_UNKNOWN)
        self.assertFalse(self.request().exists())
        hidden.chmod(0o700)
        self.assertEqual(sorted(cc.other_agents_dirs(cc.LOCAL, strict=True)),
                         sorted([str(self.home / "elsewhere"), str(self.config)]), "listed whole: every peer")
        self.assertEqual(cc._strict_confs(self.home / "none"), [], "no config root: no other agent")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads any directory")
    def test_a_config_root_that_cannot_be_looked_at_is_never_taken_for_no_peers(self):
        from ag2_sparrow.adapters.claude_code import cli as cc
        cfg = self.cfg()
        parent = cc.cm.config_root().parent
        parent.chmod(0)
        self.addCleanup(parent.chmod, 0o700)
        with self.assertRaises(PermissionError):
            cc.other_agents_dirs(cc.LOCAL, strict=True)
        with self.assertRaises(accounts.Refused) as refused:
            ac.config_dir_for_switch(cfg)
        self.assertEqual(str(refused.exception), ac.OTHERS_UNKNOWN)
        parent.chmod(0o700)
        afile = self.home / "afile"
        afile.write_text("")
        with mock.patch.object(cc.cm, "config_root", return_value=afile / "ag2-sparrow"):
            with self.assertRaises(NotADirectoryError):
                cc.other_agents_dirs(cc.LOCAL, strict=True)
            self.assertEqual(cc.other_agents_dirs(cc.LOCAL), [], "lenient: as before")
        with mock.patch.object(cc.cm, "config_root", return_value=afile):
            with self.assertRaises(NotADirectoryError):
                cc.other_agents_dirs(cc.LOCAL, strict=True)
        with mock.patch.object(cc.cm, "config_root", return_value=self.home / "missing" / "ag2-sparrow"):
            self.assertEqual(cc.other_agents_dirs(cc.LOCAL, strict=True), [], "truly missing: no peers")
            self.assertEqual(ac.config_dir_for_switch(cfg), str(self.config))

    def test_an_entry_whose_kind_cannot_be_told_refuses_rather_than_skips(self):
        from ag2_sparrow.adapters.claude_code import cli as cc

        class Entry:
            name, path = "sub", str(self.home / "sub")

            def is_dir(self, follow_symlinks=True):
                raise PermissionError("cannot stat")

        class Listing(list):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with mock.patch.object(cc.os, "scandir", return_value=Listing([Entry()])):
            with self.assertRaises(PermissionError):
                cc._strict_confs(self.home)

    def test_a_peer_that_names_this_agents_folder_another_way_is_refused_and_unknowns_fail_closed(self):
        from ag2_sparrow.adapters.claude_code import cli as cc
        cfg = self.cfg()
        link = self.home / "link-to-mine"
        link.symlink_to(self.config)
        peers = cc.cm.config_root() / "peers"
        peers.mkdir(parents=True)
        (peers / "peer.conf").write_text("CLAUDE_CONFIG_DIR=%s\n" % link)
        with self.assertRaises(accounts.Refused) as refused:
            ac.config_dir_for_switch(cfg)
        self.assertEqual(str(refused.exception), accounts.ANOTHER_AGENTS)
        with mock.patch.object(ac.accounts, "own_dir", side_effect=lambda cfg, dirs: cfg["CLAUDE_CONFIG_DIR"]):
            with self.assertRaises(accounts.Refused) as refused:
                ac.config_dir_for_switch(cfg)
            self.assertEqual(str(refused.exception), accounts.ANOTHER_AGENTS, "by inode, not by resolved text")
        (peers / "peer.conf").write_text("CLAUDE_CONFIG_DIR=%s\n" % (self.home / "not-made-yet"))
        self.assertEqual(ac.config_dir_for_switch(cfg), str(self.config), "a peer folder not there is not mine")
        self.assertFalse(ac.same_dir(self.config, self.home / "not-made-yet"))
        with mock.patch.object(ac.os, "stat", side_effect=[os.stat(self.config), PermissionError("x")]):
            with self.assertRaises(accounts.Refused):
                ac.same_dir(self.config, self.home / "elsewhere")
        with mock.patch.object(ac.os, "stat", side_effect=PermissionError("x")):
            with self.assertRaises(accounts.Refused):
                ac.same_dir(self.config, self.home / "elsewhere")

    def test_an_account_no_longer_saved_and_a_request_that_cannot_be_written(self):
        self.index(self.appdata, {})
        self.assertEqual(self.use_directly(CLAUDE_CONFIG_DIR=str(self.config)), "account-not-saved")
        self.assertEqual(self.queued()[-1]["card"]["text"], ac.NOT_SAVED)
        self.index(self.appdata, {ac.saved_key(A): A})
        with mock.patch.object(rm, "write_request", side_effect=OSError("read-only")):
            self.assertEqual(self.use_directly(CLAUDE_CONFIG_DIR=str(self.config)), "account-not-asked")
        self.assertIn("could not be written", self.queued()[-1]["card"]["text"])
        self.assertEqual(rm.outstanding(self.root), [])
        with mock.patch.object(rm.os, "link", side_effect=OSError("x")), self.assertRaises(OSError):
            rm.write_request(self.config, ac.saved_key(A), "33333333-3333-4333-8333-333333333333", 0)
        self.assertEqual(list(self.config.iterdir()), [], "the half-written request is removed")

    def test_a_watcher_that_cannot_start_is_said(self):
        with mock.patch.object(rm, "_popen", side_effect=OSError("no fork")):
            self.assertEqual(self.use_directly(CLAUDE_CONFIG_DIR=str(self.config)), "account-switching")
        self.assertIn("won't confirm here", self.queued()[-1]["card"]["text"])


class Watch(Remote):
    def clock(self, start):
        now = [start]

        def sleep(s):
            now[0] += s
        return sleep, (lambda: now[0])

    def test_switched_and_could_not_switch_are_told_in_place_and_the_receipt_taken(self):
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=True, label=A + " (max)")
        self.assertEqual(rm.watch(self.root, nonce, sleep=lambda s: None), "Switched to %s." % A)
        self.assertEqual(list(self.config.iterdir()), [], "the receipt is taken")
        self.assertEqual(self.texts(), [("$acct", "Switched to %s." % A)])
        self.assertEqual(self.queued()[0]["card"]["state"], "done")
        self.assertEqual(rm.outstanding(self.root), [])
        self.assertEqual(rm.watch(self.root, nonce), "", "told once")
        self.clear_outbox()
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=False, error="the saved sign-in\nhas expired")
        self.assertEqual(rm.watch(self.root, nonce), "Could not switch: the saved sign-in has expired")
        self.assertEqual(rm.told({"ok": False}, A), ("error", "Could not switch: the app gave no reason"))
        self.assertEqual(rm.told({"ok": True}, A), ("done", rm.UNVERIFIED % A), "no `verified`: not known verified")
        self.assertEqual(rm.told({"ok": True, "verified": True}, A), ("done", "Switched to %s." % A))

    def test_a_switch_whose_new_sign_in_is_not_tried_yet_says_so(self):
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=True, label=A, verified=False)
        said = rm.watch(self.root, nonce)
        self.assertEqual(said, rm.UNVERIFIED % A)
        self.assertTrue(said.startswith("Switched to %s. The new sign-in hasn't been tried yet" % A))
        self.assertEqual(self.texts(), [("$acct", said)])
        self.clear_outbox()
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=True, label=A, verified=None)
        (self.config / (rm.RESULT % nonce)).write_text(json.dumps({"v": 2, "nonce": nonce, "ok": True, "label": A}))
        self.assertEqual(rm.watch(self.root, nonce), rm.UNVERIFIED % A, "an older app sends no `verified`: not verified")

    def test_a_result_for_a_different_nonce_is_ignored(self):
        nonce = self.begun()
        other = "44444444-4444-4444-8444-444444444444"
        self.result(other, ok=True, label=B)
        (self.config / (rm.RESULT % nonce)).write_text(json.dumps({"v": 2, "nonce": other, "ok": True, "label": B}))
        rec = rm.load(self.root, nonce)
        self.assertIs(rm.receive(self.root, rec)["outcome"]["unreadable"], True, "a receipt naming another nonce is no answer")
        self.assertTrue((self.config / (rm.RESULT % other)).exists(), "another request's receipt is left alone")
        self.assertIsNone(rm.receive(self.root, rec), "no receipt for it any more")
        self.assertEqual(rm._parsed("not json", nonce), {"nonce": nonce, "unreadable": True})
        self.assertEqual(rm._watch(self.root, "66666666-6666-4666-8666-666666666666", None, None, print), "",
                         "a switch settled meanwhile is not told again")

    def test_an_answer_whose_card_cannot_be_queued_is_kept_and_told_on_the_next_try(self):
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=True, label=A)
        with mock.patch.object(rm.card_desk, "post", return_value=False):
            self.assertEqual(rm.watch(self.root, nonce, log=lambda *a: None), "Switched to %s." % A)
        rec, = rm.outstanding(self.root)
        self.assertEqual(rec["outcome"], {"nonce": nonce, "ok": True, "label": A, "verified": True}, "the answer is kept")
        self.assertEqual(list(self.config.iterdir()), [], "the receipt is gone")
        self.assertEqual(self.queued(), [])
        with mock.patch.object(rm.card_desk, "post", return_value=False):
            self.assertIsNone(rm.publish(self.root, self.config, ac.saved_key(B), B, DM, "$b", "", log=lambda *a: None),
                              "still owed, and still not queued: no new switch meanwhile")
            self.assertEqual(rm.settle_late(self.root, log=lambda *a: None), ["Switched to %s." % A])
        self.assertEqual(len(rm.outstanding(self.root)), 1)
        self.assertEqual(rm.recover(self.root), 0, "the supervisor's check tells it, and starts no watcher for it")
        self.assertEqual(self.texts(), [("$acct", "Switched to %s." % A)])
        self.assertEqual(rm.outstanding(self.root), [], "dropped only once queued")

    def test_a_stop_after_reading_the_receipt_before_keeping_it_still_tells_it_after_the_restart(self):
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=True, label=A)
        with mock.patch.object(rm, "save", side_effect=KeyboardInterrupt("killed")), self.assertRaises(KeyboardInterrupt):
            rm.watch(self.root, nonce)
        self.assertTrue((self.config / (rm.RESULT % nonce)).exists(), "the receipt is where the app left it")
        self.assertEqual([p.name for p in self.config.iterdir()], [rm.RESULT % nonce], "no file under another name")
        with mock.patch.object(rm, "save", side_effect=OSError("disk full")):
            self.assertIs(rm.receive(self.root, rm.load(self.root, nonce)), rm.RETRY)
        self.assertTrue((self.config / (rm.RESULT % nonce)).exists())
        self.assertEqual(rm.recover(self.root), 0, "the restart's check tells it")
        self.assertEqual(self.texts(), [("$acct", "Switched to %s." % A)])
        self.assertEqual((list(self.config.iterdir()), rm.outstanding(self.root)), ([], []))

    def test_a_stop_after_keeping_the_answer_before_removing_the_receipt_tells_it_once(self):
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=True, label=A)
        rec = rm.load(self.root, nonce)
        rm.save(self.root, dict(rec, outcome={"nonce": nonce, "ok": True, "label": A, "verified": True}))     # then the stop
        self.assertEqual(rm.settle_late(self.root), ["Switched to %s." % A])
        self.assertEqual(rm.settle_late(self.root), [])
        self.assertEqual(rm.recover(self.root), 0)
        self.assertEqual(self.texts(), [("$acct", "Switched to %s." % A)], "one card")
        self.assertEqual((list(self.config.iterdir()), rm.outstanding(self.root)), ([], []))
        with mock.patch.object(rm.Path, "unlink", side_effect=PermissionError("ro")):
            kept = dict(rec, outcome={"nonce": nonce, "ok": True})
            self.assertEqual(rm.receive(self.root, kept), kept, "a receipt that cannot be removed yet is left")

    def flaky(self, nonce, name, times=1):
        """`rm.<name>` failing `times` times as an I/O error would, for the receipt of `nonce` only."""
        real, left = getattr(rm, name), [times]

        def call(*args, **kw):
            hit = (str(args[0]).endswith(rm.RESULT % nonce) if name == "read_regular"
                   else isinstance(args[1], dict) and "outcome" in args[1])
            if hit and left[0] > 0:
                left[0] -= 1
                if name == "read_regular":
                    return None, "unreadable (EIO)"
                raise OSError(5, "Input/output error")
            return real(*args, **kw)
        return mock.patch.object(rm, name, side_effect=call)

    def test_a_passing_read_error_is_retried_by_the_watcher_never_said_failed(self):
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=True, label=A)        # the app's claim is gone; its receipt is there
        seen = []

        def sleep(s):
            seen.append(((self.config / (rm.RESULT % nonce)).exists(), len(rm.outstanding(self.root)), self.texts()))
        with self.flaky(nonce, "read_regular"):
            self.assertEqual(rm.watch(self.root, nonce, sleep=sleep), "Switched to %s." % A)
        self.assertEqual(seen, [(True, 1, [])], "both kept, nothing said, while it could not be read")
        self.assertEqual(self.texts(), [("$acct", "Switched to %s." % A)])
        self.assertEqual((list(self.config.iterdir()), rm.outstanding(self.root)), ([], []))

    def test_a_passing_save_error_is_retried_by_recovery_never_said_failed(self):
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=True, label=A)
        self.spawned.clear()
        with self.flaky(nonce, "save"):
            self.assertEqual(rm.recover(self.root), 1, "still waiting: its watcher is started again")
        self.assertEqual(self.texts(), [])
        self.assertTrue((self.config / (rm.RESULT % nonce)).exists(), "the receipt is kept")
        self.assertNotIn("outcome", rm.load(self.root, nonce))
        self.assertEqual(rm.recover(self.root), 0)
        self.assertEqual(self.texts(), [("$acct", "Switched to %s." % A)])
        self.assertEqual((list(self.config.iterdir()), rm.outstanding(self.root)), ([], []))

    def test_errors_that_do_not_pass_are_still_waited_out_and_never_said_failed(self):
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=True, label=A)
        sleep, clock = self.clock(rm.load(self.root, nonce)["at"])
        with self.flaky(nonce, "read_regular", times=10 ** 6):
            self.assertEqual(rm.settle_late(self.root), [])
            self.assertEqual(rm.watch(self.root, nonce, sleep=sleep, clock=clock), rm.UNTOLD)
        self.assertEqual([t for _, t in self.texts()], [rm.UNTOLD])
        self.assertTrue(rm.load(self.root, nonce)["gave_up"], "kept for a later look")
        self.assertEqual(rm.settle_late(self.root), ["Switched to %s." % A], "told once it reads")
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=True, label=A)
        late = []
        with mock.patch.object(rm, "receive", side_effect=lambda root, rec: late.append(1) or (
                None if len(late) == 1 else rm.RETRY)):
            (self.config / (rm.RESULT % nonce)).unlink()
            self.assertEqual(rm.settle_late(self.root), [], "a receipt that turns up unreadable waits")
        with mock.patch.object(rm, "receive", side_effect=[None, rm.RETRY, None, None]):
            self.assertEqual(rm.watch(self.root, nonce, sleep=lambda s: None), rm.UNTOLD)

    def test_a_receipt_with_its_nonce_but_not_the_contracts_shape_is_said_uncertain(self):
        n = "88888888-8888-4888-8888-888888888888"
        bad = ({}, {"ok": "yes", "label": A}, {"ok": None}, {"ok": True}, {"ok": True, "label": 5},
               {"ok": True, "label": " "}, {"ok": True, "label": A, "verified": "no"},
               {"ok": False, "verified": 1}, {"unknown": "yes", "ok": False}, {"unknown": False})
        for data in bad:
            self.assertEqual(rm._parsed(json.dumps(dict(data, nonce=n)), n), {"nonce": n, "unreadable": True}, data)
        good = ({"ok": True, "label": A}, {"ok": True, "label": A, "verified": False}, {"ok": False},
                {"ok": False, "error": "locked"}, {"unknown": True}, {"unknown": True, "ok": False, "error": "x"},
                {"unknown": False, "ok": False})
        for data in good:
            self.assertNotIn("unreadable", rm._parsed(json.dumps(dict(data, nonce=n)), n), data)
        self.assertEqual(rm.told(rm._parsed(json.dumps({"nonce": n, "ok": True, "label": A}), n), A)[1],
                         rm.UNVERIFIED % A, "an older app's success: usable, shown as not yet tried")
        nonce = self.begun()
        self.claim_it(nonce)
        (self.config / (rm.RESULT % nonce)).write_text(json.dumps({"v": 2, "nonce": nonce, "label": A}))
        (self.config / (rm.CLAIMED % nonce)).unlink()
        said = rm.watch(self.root, nonce)
        self.assertEqual(said, rm.UNREAD_ON % ac.account_switch.masked(SARAH), "no `ok`: never 'Could not switch'")

    def test_a_receipt_read_whole_but_not_an_answer_is_said_so(self):
        nonce = self.begun()
        self.claim_it(nonce)
        (self.config / (rm.CLAIMED % nonce)).unlink()
        (self.config / (rm.RESULT % nonce)).write_text("{half")
        said = rm.watch(self.root, nonce)
        self.assertEqual(said, rm.UNREAD_ON % ac.account_switch.masked(SARAH), "uncertain, with the account now")
        self.assertNotIn("Could not switch", said)
        self.assertEqual(self.texts(), [("$acct", said)])
        self.assertEqual((list(self.config.iterdir()), rm.outstanding(self.root)), ([], []), "removed once told")
        self.clear_outbox()
        nonce = self.begun()
        (self.config / rm.REQUEST).unlink()
        (self.config / (rm.RESULT % nonce)).write_text("[]")
        self.answer = "not json"
        self.assertEqual(rm.watch(self.root, nonce), rm.UNREAD)

    def aged(self, nonce, by):
        rm.save(self.root, dict(rm.load(self.root, nonce), at=rm.load(self.root, nonce)["at"] - by))

    def test_a_claim_unanswered_past_its_bound_is_told_once_kept_and_never_removed(self):
        nonce = self.begun()
        self.claim_it(nonce)
        rm.save(self.root, dict(rm.load(self.root, nonce), gave_up=True))     # the watcher stopped at 10 min
        self.aged(nonce, rm.STALE_CLAIM_S - 60)
        self.assertEqual(rm.settle_late(self.root), [], "within its bound: still the app's")
        self.aged(nonce, 120)
        said = rm.UNCERTAIN % A
        self.assertEqual(rm.recover(self.root), 0)
        self.assertEqual(rm.settle_late(self.root), [], "told once")
        self.assertEqual(self.texts(), [("$acct", said)])
        self.assertNotIn("Could not", said)
        self.assertTrue((self.config / (rm.CLAIMED % nonce)).exists(), "sparrow never removes a claim")
        self.assertTrue(rm.load(self.root, nonce)["told_uncertain"], "the record is kept")
        self.assertTrue(rm.busy(self.root, self.config))
        os.utime(self.config / (rm.CLAIMED % nonce), (time.time() - rm.STALE_CLAIM_S - 5,) * 2)
        self.assertEqual(rm.busy_words(self.root, self.config), rm.UNRESOLVED)
        self.posted("$b")
        rc, out = self.tap("task-b", "Use " + B, event="$b")
        self.assertIn("(account-busy)", out["refused"])
        self.assertEqual(self.texts()[-1], ("$b", rm.UNRESOLVED))
        self.clear_outbox()
        self.result(nonce, ok=True, label=A)          # the app, reopened, answers
        self.assertEqual(rm.settle_late(self.root), ["Switched to %s after all." % A])
        self.assertEqual((list(self.config.iterdir()), rm.outstanding(self.root)), ([], []))

    def test_a_late_answer_after_the_uncertain_line_says_its_real_outcome(self):
        self.assertEqual(rm.told({"ok": True}, A, after_all=True)[1], rm.UNVERIFIED % (A + " after all"))
        self.assertEqual(rm.told({"ok": False, "error": "locked"}, A, after_all=True)[1], "Could not switch: locked")
        nonce = self.begun()
        self.claim_it(nonce)
        rm.save(self.root, dict(rm.load(self.root, nonce), gave_up=True, told_uncertain=True))
        (self.config / (rm.CLAIMED % nonce)).unlink()         # released by the app with no receipt: nothing to say
        self.assertEqual(rm.settle_late(self.root), [])
        self.assertEqual((self.texts(), rm.outstanding(self.root)), ([], []))
        self.assertEqual(rm.busy_words(self.root, self.config), rm.BUSY)

    def test_the_uncertain_line_survives_a_failed_journal_write_and_a_restart_between_steps(self):
        nonce = self.begun()
        self.claim_it(nonce)
        rm.save(self.root, dict(rm.load(self.root, nonce), gave_up=True))
        self.aged(nonce, rm.STALE_CLAIM_S + 1)
        real = rm.save

        def failing(root, rec):
            if rec.get("told_uncertain"):
                raise OSError("disk full")
            return real(root, rec)
        with mock.patch.object(rm, "save", side_effect=failing):
            self.assertEqual(rm.settle_late(self.root, log=lambda *a: None), [rm.UNCERTAIN % A])
        self.assertNotIn("told_uncertain", rm.load(self.root, nonce))
        self.assertEqual(rm.settle_late(self.root), [rm.UNCERTAIN % A], "the second upkeep journals it")
        self.assertEqual(len(self.queued()), 1, "one card: the repeat is the same keyed card")
        self.assertTrue(rm.load(self.root, nonce)["told_uncertain"])
        with mock.patch.object(rm.card_desk, "post", return_value=False):
            rm.save(self.root, dict(rm.load(self.root, nonce), told_uncertain=False))
            self.assertEqual(rm.settle_late(self.root, log=lambda *a: None), [], "not queued: not marked told")
            self.assertFalse(rm.load(self.root, nonce)["told_uncertain"])
        rec = {k: v for k, v in rm.load(self.root, nonce).items() if k != "uncertain_at"}
        rm.save(self.root, rec)
        with mock.patch.object(rm, "save", side_effect=OSError("disk full")):
            self.assertEqual(rm.settle_late(self.root, log=lambda *a: None), [], "not journalled: not told")
        self.assertEqual(len(self.queued()), 1)
        self.result(nonce, ok=True, label=A)          # a real receipt stays the answer, whatever came before
        self.assertEqual(rm.settle_late(self.root), ["Switched to %s." % A])

    def test_a_junk_request_file_stops_blocking_after_its_bound_and_is_removed_only_while_unchanged(self):
        self.request().write_text("")
        self.assertTrue(rm.busy(self.root, self.config), "fresh: it may be the app's, mid-write")
        self.assertIsNone(rm.publish(self.root, self.config, "a" * 16, A, DM, "$x", str(self.bridge)))
        old = time.time() - rm.JUNK_REQUEST_S - 5
        os.utime(self.request(), (old, old))
        self.assertFalse(rm.busy(self.root, self.config))
        nonce = rm.publish(self.root, self.config, "a" * 16, A, DM, "$x", str(self.bridge))
        self.assertEqual(rm.request_nonce(self.config), nonce, "removed, and the new request written")
        rm.drop(self.root, nonce)
        self.request().unlink()
        for junk in ("{half", '{"v": 2}'):
            self.request().write_text(junk)
            os.utime(self.request(), (old, old))
            self.assertIsNotNone(rm.junk_request(self.config))
        real = rm.os.rename

        def swapped(src, dst):
            real(src, dst)
            Path(dst).write_text("changed")         # another file took its place meanwhile
        with mock.patch.object(rm.os, "rename", side_effect=swapped):
            self.assertFalse(rm.clear_junk_request(self.config))
        self.assertEqual(self.request().read_text(), "changed", "put back")
        os.utime(self.request(), (old, old))
        with mock.patch.object(rm.os, "rename", side_effect=swapped), \
                mock.patch.object(rm.os, "link", side_effect=FileExistsError("x")):
            self.assertFalse(rm.clear_junk_request(self.config))
        self.request().write_text('{"nonce": "x"}')
        os.utime(self.request(), (old, old))
        self.assertIsNone(rm.junk_request(self.config), "a whole request is never junk")
        self.request().unlink()
        self.assertFalse(rm.clear_junk_request(self.config))
        with mock.patch.object(rm, "request_nonce", return_value=""):
            self.assertIsNone(rm.junk_request(self.config), "gone between the two looks")
        self.request().write_text("")
        os.utime(self.request(), (old, old))
        with mock.patch.object(rm.os, "rename", side_effect=OSError("x")):
            self.assertFalse(rm.clear_junk_request(self.config))
        self.assertEqual(rm.claims(self.home / "none"), [])
        self.assertIsNone(rm.claim_age(self.config, "nope"))

    def test_the_watchers_log_and_the_records_are_owner_only_whatever_the_umask(self):
        log = Path(self.root) / "state" / rm.LOG
        log.write_text("")
        log.chmod(0o644)
        old = os.umask(0)
        try:
            self.assertTrue(rm.start(self.root, "77777777-7777-4777-8777-777777777777", popen=lambda *a, **k: None))
            nonce = self.begun()
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        tmp = rm.folder(self.root) / (nonce + ".json.tmp")
        tmp.write_text("")
        tmp.chmod(0o644)
        rm.save(self.root, rm.load(self.root, nonce))
        self.assertEqual(stat.S_IMODE((rm.folder(self.root) / (nonce + ".json")).stat().st_mode), 0o600)
        with mock.patch.object(rm.os, "fchmod", side_effect=OSError("x")), self.assertRaises(OSError):
            rm.private_log(self.root)

    def test_an_app_restart_mid_switch_says_the_account_now_never_that_it_failed(self):
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=False, unknown=True, error="the app restarted during the switch")
        said = rm.watch(self.root, nonce)
        self.assertEqual(said, rm.RESTARTED_ON % ac.account_switch.masked(SARAH))
        self.assertNotIn("Could not", said)
        self.assertEqual(self.queued()[-1]["card"]["text"], said)
        self.clear_outbox()
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=False, unknown=True, error="the app restarted during the switch")
        self.answer = "not json"        # the account cannot be read
        self.assertEqual(rm.watch(self.root, nonce), rm.RESTARTED)
        self.answer = {"loggedIn": True, "email": SARAH}
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=False, error="locked")
        self.assertEqual(rm.watch(self.root, nonce), "Could not switch: locked", "a plain failure is still one")

    def test_unclaimed_after_90_seconds_is_withdrawn_only_while_it_is_its_own(self):
        nonce = self.begun()
        at = rm.load(self.root, nonce)["at"]
        sleep, clock = self.clock(at)
        self.assertEqual(rm.watch(self.root, nonce, sleep=sleep, clock=clock), rm.UNCLAIMED)
        self.assertGreaterEqual(clock(), at + rm.UNCLAIMED_S)
        self.assertFalse(self.request().exists())
        self.assertEqual(self.texts(), [("$acct", rm.UNCLAIMED)])
        self.assertEqual(rm.outstanding(self.root), [])
        other = "55555555-5555-4555-8555-555555555555"
        rm.write_request(self.config, "a" * 16, other, 0)
        self.assertFalse(rm.withdraw(self.config, nonce), "another nonce's request is not taken")
        with mock.patch.object(rm.os, "link", side_effect=FileExistsError("newer")):
            self.assertFalse(rm.withdraw(self.config, nonce))
        self.assertFalse(self.request().exists(), "nothing newer is ever overwritten")
        rm.write_request(self.config, "a" * 16, other, 0)
        self.assertEqual(rm.request_nonce(self.config), other)
        self.request().write_text("not json")
        self.assertFalse(rm.withdraw(self.config, nonce))
        self.assertEqual(rm.request_nonce(self.config), "")
        self.request().unlink()
        self.assertFalse(rm.withdraw(self.config, nonce), "claimed meanwhile")

    def test_claimed_past_90_seconds_is_in_progress_never_failed_then_untold_and_kept(self):
        nonce = self.begun()
        self.claim_it(nonce)
        at = rm.load(self.root, nonce)["at"]
        sleep, clock = self.clock(at)
        said = rm.watch(self.root, nonce, sleep=sleep, clock=clock)
        self.assertEqual(said, rm.UNTOLD)
        self.assertEqual([t for _, t in self.texts()], [rm.WORKING % A, rm.UNTOLD])
        self.assertFalse(any("Could not" in t for _, t in self.texts()), "never reported failed")
        rec, = rm.outstanding(self.root)
        self.assertTrue(rec["gave_up"], "the nonce is kept for a late result")
        self.assertEqual(rm.recover(self.root), 0, "a watcher that gave up is not restarted")

    def test_a_late_result_is_told_on_the_next_card_after_the_watcher_gave_up(self):
        nonce = self.begun()
        self.claim_it(nonce)
        rm.save(self.root, dict(rm.load(self.root, nonce), gave_up=True))
        self.result(nonce, ok=True, label=A)
        self.clear_outbox()
        rc, out = self.claim(self.task("task-late", "/account"))
        self.assertIn("(account-card)", out["refused"])
        told, fresh = self.texts()
        self.assertEqual(told, ("$acct", "Switched to %s." % A), "told in place of the card tapped, first")
        self.assertIsNone(fresh[0], "then the new card")
        self.assertEqual(rm.outstanding(self.root), [])

    def test_a_late_result_is_told_on_the_next_tap_which_then_goes_ahead(self):
        nonce = self.begun()
        self.claim_it(nonce)
        rm.save(self.root, dict(rm.load(self.root, nonce), gave_up=True))
        self.result(nonce, ok=False, error="locked")
        self.clear_outbox()
        new = rm.publish(self.root, self.config, ac.saved_key(B), B, DM, "$b", str(self.bridge))
        self.assertTrue(new and new != nonce)
        self.assertEqual(self.texts(), [("$acct", "Could not switch: locked")])

    def test_a_request_the_app_no_longer_holds_in_any_form_is_settled(self):
        nonce = self.begun()
        self.request().unlink()
        self.assertEqual(rm.settle_late(self.root), [rm.UNTOLD])
        self.assertEqual(rm.outstanding(self.root), [])
        nonce = self.begun()
        rm.save(self.root, dict(rm.load(self.root, nonce), gave_up=True))
        self.request().unlink()
        self.clear_outbox()
        self.assertEqual(rm.settle_late(self.root), [], "one already said untold is dropped quietly")
        nonce = self.begun()
        self.request().unlink()
        self.assertEqual(rm.watch(self.root, nonce), rm.UNTOLD, "the watcher too")
        nonce = self.begun()
        late = dict(rm.load(self.root, nonce), outcome={"nonce": nonce, "ok": True, "label": A, "verified": True})
        with mock.patch.object(rm, "receive", side_effect=[None, late]):
            self.request().unlink()
            self.assertEqual(rm.settle_late(self.root), ["Switched to %s." % A], "written as the claim went")
        nonce = self.begun()
        self.request().unlink()
        late = dict(rm.load(self.root, nonce), outcome={"nonce": nonce, "ok": True, "label": A, "verified": True})
        with mock.patch.object(rm, "receive", side_effect=[None, late]):
            self.assertEqual(rm.watch(self.root, nonce), "Switched to %s." % A)

    def test_restart_recovery_starts_a_watcher_for_an_outstanding_nonce(self):
        nonce = self.begun()
        self.spawned.clear()
        self.assertEqual(rm.recover(self.root), 1)
        argv, = self.spawned
        self.assertEqual(argv[-2:], ["--nonce", nonce])
        fd = rm.os.open(str(rm.folder(self.root) / (nonce + ".lock")), rm.os.O_RDWR)
        self.addCleanup(rm.os.close, fd)
        self.assertTrue(rm.try_lock_fd(fd))
        held = rm.watch(self.root, nonce)      # flock is per open file: a second open conflicts
        self.assertEqual(held, "", "a second watcher leaves it to the first")
        self.assertEqual(rm.recover(self.root), 0, "a live watcher is not doubled")
        self.assertTrue(rm.watcher_alive(self.root, nonce))
        self.assertEqual(rm.settle_late(self.root), [], "a watched switch is the watcher's to tell")

    def test_odd_records_and_the_detached_command(self):
        self.assertIsNone(rm.load(self.root, "../x"))
        self.assertEqual(rm.watch(self.root, "../x"), "")
        nonce = self.begun()
        (rm.folder(self.root) / (nonce + ".json")).write_text("not json")
        self.assertIsNone(rm.load(self.root, nonce))
        self.assertEqual(rm.outstanding(self.root), [])
        rm.drop(self.root, nonce)
        self.request().unlink()
        nonce = self.begun()
        self.claim_it(nonce)
        self.result(nonce, ok=True, label=A)
        with mock.patch("builtins.print") as said:
            self.assertEqual(rm.main(["await", "--root", self.root, "--nonce", nonce]), 0)
            self.assertEqual(rm.main(["await", "--root", self.root, "--nonce", nonce]), 0)
        self.assertEqual([c.args[0] for c in said.call_args_list], ["Switched to %s." % A, "nothing to watch"])
        with mock.patch.object(rm.card_desk, "post", return_value=False):
            nonce = self.begun()
            self.result(nonce, ok=True, label=A)
            self.request().unlink()
            self.assertEqual(rm.watch(self.root, nonce, log=lambda *a: None), "Switched to %s." % A)
        self.assertEqual(rm.outstanding(self.home / "none"), [])
        self.assertFalse(rm.claimed_any(self.home / "none"))
        with mock.patch.object(rm.os, "open", side_effect=OSError("x")):
            self.assertFalse(rm.watcher_alive(self.root, nonce))
        with mock.patch.object(ac.remote, "settle_late", side_effect=OSError("ro")):
            self.posted("$ro")

    def test_the_supervisor_restarts_a_dead_watcher_every_so_often_and_never_stops_over_it(self):
        from ag2_sparrow.adapters.claude_code import launcher
        logged, now = [], [100.0]
        sup = mock.Mock(root=self.root, clock=lambda: now[0], log=logged.append, spec=["root", "clock", "log"])
        self.begun()
        self.spawned.clear()
        launcher.Launcher.account_switches(sup)
        launcher.Launcher.account_switches(sup)
        self.assertEqual(len(self.spawned), 1, "checked once per ACCOUNT_SWITCH_CHECK_S")
        now[0] += launcher.ACCOUNT_SWITCH_CHECK_S
        with mock.patch.object(rm, "recover", side_effect=RuntimeError("broken")):
            launcher.Launcher.account_switches(sup)
        self.assertEqual(logged[0], "account switch: its watcher restarted")
        self.assertIn("not recovered (RuntimeError: broken)", logged[1])




LINK = "https://claude.ai/oauth/authorize?code=true&client_id=x&state=s"
CODE = "abcdEFGH1234#stateXYZ987"


class PhoneSignIn(Remote):
    """Owner 11-27: a new Claude account signed in from the phone. The card starts Claude Code's two-step
    sign-in here and shows its link; the code comes back typed into the card (the tap carries it, never the
    chat's text) or as the owner's own DM message. Never the model's to start or finish."""

    def setUp(self):
        super().setUp()
        self.started, self.given, self.waiting = [], [], False

        def start(cfg, env, state_dir, **kw):
            self.started.append((env.get("CLAUDE_CONFIG_DIR"), str(state_dir)))
            self.waiting = True
            return LINK

        def give(state_dir, code, **kw):
            self.given.append(code)
            self.waiting = False
            return True, ""

        for name, fake in (("start", start), ("give_code", give), ("waiting", lambda d: self.waiting)):
            p = mock.patch.object(accounts, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def started_card(self):
        self.posted()
        rc, out = self.tap("task-si", ac.SIGN_IN_LABEL)
        self.assertIn("(account-signin-started)", out["refused"])
        card = self.queued()[-1]["card"]
        _, issued = connector_cards._core()
        issued.record(Path(self.root), "$acct", card, room=DM, lane="")    # posted in place, as the bridge records it
        self.clear_outbox()
        return card

    def test_sign_in_shows_the_page_and_a_code_box_whose_button_carries_the_code(self):
        card = self.started_card()
        self.assertEqual(self.started, [(str(self.config), str(Path(self.root) / "state" / "account-sign-in"))])
        self.assertEqual(card["input"]["name"], ac.CODE_INPUT)
        self.assertEqual([(b["kind"], b["label"]) for b in card["buttons"]],
                         [("url", "Open sign-in page"), ("reply", "Submit code")])
        self.assertEqual(card["buttons"][0]["url"], LINK)
        self.assertTrue(card["buttons"][1]["uses_input"])
        self.assertNotIn("on the computer", card["text"])

    def test_the_code_typed_into_the_card_signs_in_and_is_never_in_the_chat_text(self):
        self.started_card()
        rc, out = self.claim(self.task("task-code", "Submit code", reply_to="$acct",
                                       click={"card_id": ac.account_switch.NOTE, "action": "Submit code",
                                              "input": {"code": CODE}}))
        self.assertIn("(account-signed-in)", out["refused"])
        self.assertEqual(self.given, [CODE])
        done = self.queued()[-1]["card"]
        self.assertIn("Signed in.", done["text"])
        self.assertNotIn(CODE, json.dumps(self.queued()))

    def test_the_code_sent_as_a_dm_message_signs_in_and_says_the_message_can_be_deleted(self):
        self.started_card()
        rc, out = self.claim(self.task("task-typed", CODE))
        self.assertIn("(account-signed-in)", out["refused"], "taken here, never the model's")
        self.assertEqual(self.given, [CODE])
        self.assertIn(ac.TYPED_NOTE, self.queued()[-1]["card"]["text"])

    def test_no_code_is_taken_when_no_sign_in_waits_or_from_anyone_but_the_owner_in_her_dm(self):
        rc, out = self.claim(self.task("task-n1", CODE))
        self.assertNotIn("account-signed-in", json.dumps(out), "no sign-in is waiting: ordinary words")
        self.started_card()
        for tid, kw in (("task-n2", {"tier": "team", "sender": "@eve:s.space"}), ("task-n3", {"kind": "room"})):
            rc, out = self.claim(self.task(tid, CODE, **kw))
            self.assertNotIn("account-signed-in", json.dumps(out), kw)
        rc, out = self.claim(self.task("task-n4", "thanks, signing in now"))
        self.assertNotIn("account-signed-in", json.dumps(out), "words that are not a code")
        self.assertEqual(self.given, [])

    def test_a_click_carries_its_input_only_when_it_is_one(self):
        from ag2_sparrow import agent_card
        click = {"card_id": "c", "action": "a"}
        self.assertEqual(agent_card.card_click({"card_click": json.dumps(dict(click, input={"code": CODE}))})["input"],
                         {"code": CODE})
        for bad in ({"9x": "v"}, {"code": 7}, {"code": "x" * 3000}, {}, "code", {str(i): "v" for i in range(9)}):
            self.assertNotIn("input", agent_card.card_click({"card_click": json.dumps(dict(click, input=bad))}), bad)

if __name__ == "__main__":
    unittest.main()
