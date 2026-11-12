#!/usr/bin/env python3
"""The owner's "show my …" cards (skills/agent-cards/scripts/cards.py) and their reply handlers.

- Each builder's card passes the client's checks (agent_card.problem): connections (core rows only,
  workers' own agents as a footer, chat-app bridges), workers, apps, and circle with an honest empty state.
- `show` posts only for the owner, only in their checked DM, through the injected Action call.
- `reply` acts only for the owner in their DM; Disconnect and Retire act only on their confirmed variant;
  app replies never disconnect an app.

Every source and every action is a fake; nothing touches the network, tmux or the real workspace.

Run: python3 tests/skills/agent-cards/cards.test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("cards", ROOT / "skills" / "agent-cards" / "scripts" / "cards.py")
cards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cards)
ac = cards.ac

DM, ROOM, OWNER = "!dm:s.space", "!team:s.space", "@owner:s.space"
CORE = {"instance": "s_dspace", "owner_mxid": OWNER, "agent_mxid": "@owner-mini.agent:s.space"}
ROWS = [
    {"instance": "s_dspace", "server": "s.space", "primary": True, "state": "connected",
     "agent_mxid": "@owner-mini.agent:s.space", "agent_display_name": "Mini"},
    {"instance": "dev-s-space", "server": "dev.s.space", "primary": False, "state": "connected",
     "agent_mxid": "@owner-mini.agent:dev.s.space"},
    {"instance": "", "server": "ag2.space", "primary": False, "state": "failed"},
    {"instance": "s-space-w1", "server": "s.space", "primary": False, "state": "connected", "worker": "w1"},
]
BRIDGES = [{"channel": "discord", "name": "Discord", "running": False},
           {"channel": "slack", "name": "Slack", "running": True}]
WORKERS = [{"worker_id": "w1", "name": "Pylon", "state": "live", "paused": False, "session_alive": True},
           {"worker_id": "w2", "name": "Quill", "state": "live", "paused": True, "session_alive": False},
           {"worker_id": "w3", "name": "Old", "state": "retired", "paused": False, "session_alive": False}]
APPS = [{"id": "c1", "toolkit": "gmail", "name": "Gmail", "status": "ACTIVE", "accountLabel": "me@work.io",
         "isDefault": True},
        {"id": "c2", "toolkit": "linear", "name": "Linear", "status": "EXPIRED"},
        {"id": "c3", "toolkit": "Bad Slug", "status": "ACTIVE"}]


def task(said, *, tier="owner", user=OWNER, kind="dm", room=DM, **extra):
    h = {"id": "task-1", "task_layout": "mid"}
    tail = {"source": "ag2space", "channel_id": room, "channel_kind": kind, "user_id": user, "access_tier": tier,
            "source_message_id": "$ask", "task_id": "task-1", **extra}
    return "\n".join([f"{k}: {v}" for k, v in h.items()] + [f"task: {said}"]
                     + [f"{k}: {v}" for k, v in tail.items()]) + "\n"


def actions(card):
    return [a for g in card.get("groups") or [] for r in g["rows"] for a in r.get("actions") or []]


def menu_items(card):
    return [i for a in actions(card) if a["kind"] == "menu" for i in a["items"]]


class Builders(unittest.TestCase):
    def test_connections(self):
        body, card = cards.connections_card(ROWS, BRIDGES)
        self.assertIsNone(ac.problem(card))
        self.assertNotIn("\n", body)
        servers, chat = card["groups"]
        self.assertEqual([r["title"] for r in servers["rows"]], ["Mini", "dev.s.space", "ag2.space"])
        self.assertEqual(servers["rows"][0]["tag"], "Main")
        self.assertEqual([(r["title"], r["state"]) for r in chat["rows"]], [("Discord", "off"), ("Slack", "on")])
        self.assertEqual(card["footer_link"]["reply"], "show my workers")
        self.assertIn("1 worker has its own agent", card["footer_link"]["label"])
        replies = {i["command"]: i for i in menu_items(card)}
        self.assertEqual(replies["card:connections:dev-s-space:make-main"]["reply"], "Make dev.s.space my main connection")
        self.assertIn("card:connections:dev-s-space:make-main", replies)
        disconnect = replies["card:connections:dev-s-space:disconnect:confirmed"]
        self.assertIn("confirm", disconnect)
        self.assertFalse(any("s_dspace" in r for r in replies), "the main connection offers no Disconnect")
        self.assertEqual([b["label"] for b in card["header"]["badges"]], ["s.space", "dev.s.space", "ag2.space"])

    def test_workers(self):
        _, card = cards.workers_card(WORKERS, {"w1": {"agent_mxid": "@owner-pylon.agent:s.space", "server": "s.space"}},
                                     {"w1": "codex", "w2": "claude"})
        self.assertIsNone(ac.problem(card))
        rows = card["groups"][0]["rows"]
        self.assertEqual([(r["title"], r["tag"]) for r in rows], [("Pylon", "Codex"), ("Quill", "Claude"),
                                                                   ("Old", "Claude")])
        self.assertEqual(rows[0]["actions"][0], {"label": "Message", "kind": "open_dm",
                                                 "user_id": "@owner-pylon.agent:s.space"})
        self.assertNotIn("actions", rows[2])
        items = {i["command"]: i for i in menu_items(card)}
        self.assertEqual(items["card:workers:w1:pause"]["reply"], "Pause Pylon")
        self.assertIn("card:workers:w1:pause", items)
        self.assertNotIn("card:workers:w2:pause", items)
        self.assertIn("confirm", items["card:workers:w1:retire:confirmed"])
        self.assertEqual(card["buttons"][0]["reply"], "create a worker")

    def test_apps(self):
        _, card = cards.apps_card(APPS)
        self.assertIsNone(ac.problem(card))
        rows = card["groups"][0]["rows"]
        self.assertEqual([(r["title"], r["subtitle"], r["avatar"]["app"]) for r in rows],
                         [("Gmail", "me@work.io · default", "gmail"), ("Linear", "Signed out", "linear")])
        self.assertEqual((rows[1]["actions"][0]["reply"], rows[1]["actions"][0]["command"]),
                         ("Reconnect Linear", "card:apps:linear:reconnect"))
        self.assertEqual(card["buttons"][0]["reply"], "connect an app")

    def test_circle_and_its_empty_state(self):
        _, empty = cards.circle_card([])
        self.assertEqual(empty["text"], "I haven't recorded who I work with yet.")
        self.assertIsNone(ac.problem(empty))
        _, card = cards.circle_card([{"entity_id": "@ana:s.space", "kind": "human", "one_line": "reviews"},
                                     {"entity_id": "@ana-bot.agent:s.space", "kind": "agent"},
                                     {"entity_id": "svc", "kind": "service"}])
        self.assertIsNone(ac.problem(card))
        people, agents = card["groups"]
        self.assertEqual(people["rows"][0]["avatar"], {"mxid": "@ana:s.space", "shape": "round"})
        self.assertEqual(agents["rows"][0]["avatar"]["shape"], "square")

    def test_nothing_set_up(self):
        for body, card in (cards.connections_card([], []), cards.workers_card([], {}, {}), cards.apps_card([])):
            self.assertIsNone(ac.problem(card))
            self.assertTrue(card["text"])


class Fake:
    def __init__(self):
        self.calls = []

    def execute(self, action, arguments, *, operation_id="", room_id=""):
        self.calls.append((action, arguments, operation_id, room_id))
        return {"result": {"event_id": "$card"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        p = mock.patch.object(cards.connections, "owner_dm", lambda row, ws=None: DM)
        p.start()
        self.addCleanup(p.stop)

    def run_(self, argv, text, **kw):
        path = self.ws / "task.txt"
        path.write_text(text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cards.main(["--workspace", str(self.ws), *argv, "--task-file", str(path)], core=CORE, **kw)
        return rc, json.loads(out.getvalue())


class Show(Base):
    def test_the_owner_gets_the_card_in_their_dm(self):
        fake = Fake()
        with mock.patch.object(cards, "read_connections", lambda ws: (ROWS, BRIDGES)):
            rc, out = self.run_(["show", "connections"], task("show my connections"), execute=fake.execute)
        self.assertEqual((rc, out), (0, {"status": "posted", "event_id": "$card"}))
        action, args, op, room = fake.calls[0]
        self.assertEqual((action, op, room, args["reply_to"]), ("room.message.send", "task-1:connections-card", DM, "$ask"))
        self.assertEqual(args["extra_content"]["space.ag2.card"]["id"], "connections")

    def test_anyone_else_or_any_other_room_is_refused_before_anything_is_read(self):
        fake = Fake()
        read = mock.Mock(side_effect=AssertionError("read"))
        with mock.patch.object(cards, "build_kind", read):
            for text in (task("x", tier="team"), task("x", user="@x:s.space"), task("x", kind="room", room=ROOM),
                         task("x", room=ROOM), task("x", collaborator="true")):
                rc, out = self.run_(["show", "apps"], text, execute=fake.execute)
                self.assertEqual((rc, out["status"]), (2, "refused"))
        self.assertEqual(fake.calls, [])


class Sources(Base):
    """Each card reads its own source on this machine, and `show` names what could not be read or sent."""

    def test_the_circle_is_read_from_the_collaboration_map_and_skips_what_it_cannot_show(self):
        self.assertEqual(cards.read_circle(self.ws), [])
        path = self.ws / "data" / "collaboration-intelligence" / "quick-lookup.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("quick_lookup:\n  recent_entities:\n    - {entity_id: '@ana:s.space', kind: human}\n"
                        "    - just a name\n    - {entity_id: svc, kind: service}\n    - {name: Bo, kind: agent}\n")
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.assertEqual(cards.read_circle(self.ws), [])
            return
        entities = cards.read_circle(self.ws)
        self.assertEqual(len(entities), 4)
        _, card = cards.build_kind("circle", self.ws)
        people, agents = card["groups"]
        self.assertEqual([r["title"] for r in people["rows"] + agents["rows"]], ["@ana:s.space", "Bo"])
        self.assertEqual(agents["rows"][0]["avatar"], {"initial": "B", "shape": "square"})
        path.write_text(": : not yaml : [")
        self.assertEqual(cards.read_circle(self.ws), [])

    def test_the_circle_reads_a_wrapped_or_bare_map_and_skips_a_missing_or_broken_one(self):
        path = self.ws / "data" / "collaboration-intelligence" / "quick-lookup.yaml"
        self.assertEqual(cards.read_circle(self.ws, load=json.loads), [])
        path.parent.mkdir(parents=True)
        ana = {"entity_id": "@ana:s.space", "kind": "human"}
        for doc in ({"quick_lookup": {"recent_entities": [ana]}}, {"recent_entities": [ana]}):
            path.write_text(json.dumps(doc))
            self.assertEqual(cards.read_circle(self.ws, load=json.loads), [ana], doc)
        for text in ("{broken", "[1, 2]", '{"quick_lookup": [1]}', "null"):
            path.write_text(text)
            self.assertEqual(cards.read_circle(self.ws, load=json.loads), [], text)

    def test_each_kind_reads_its_own_source(self):
        import chat_bridges
        import connectors
        import pool_manage
        import pool_roster
        with mock.patch.object(cards.connections, "list_connections", lambda ws: ROWS), \
                mock.patch.object(cards.connections, "status_for", lambda r, ws: r), \
                mock.patch.object(chat_bridges, "states", lambda d, env: BRIDGES):
            self.assertEqual(cards.build_kind("connections", self.ws)[1]["id"], "connections")
        with mock.patch.object(pool_manage, "listing", lambda ws: WORKERS), \
                mock.patch.object(pool_roster, "load_roster", lambda ws: {"workers": {"w1": {"runtime": "codex"}}}), \
                mock.patch.object(cards.connections, "worker_identity", lambda wid, ws: None):
            workers, identities, runtimes = cards.read_workers(self.ws)
            self.assertEqual((len(workers), identities["w1"], runtimes), (3, {}, {"w1": "codex"}))
            self.assertEqual(cards.build_kind("workers", self.ws)[1]["id"], "workers")
        with mock.patch.object(connectors.Cloud, "connection_rows", lambda self, cached=False: APPS):
            self.assertEqual(cards.build_kind("apps", self.ws)[1]["id"], "apps")

    def test_print_shows_the_card_without_posting_it(self):
        with mock.patch.object(cards, "read_apps", lambda ws: APPS):
            rc, out = self.run_(["show", "apps", "--print"], task("show my apps"))
        self.assertEqual((rc, out["card"]["id"]), (0, "apps"))
        self.assertFalse(cards.issued_cards.ledger_path(self.ws).exists())

    def test_a_source_or_a_send_that_fails_is_named(self):
        with mock.patch.object(cards, "read_apps", side_effect=RuntimeError("cloud down")):
            rc, out = self.run_(["show", "apps"], task("show my apps"), execute=Fake().execute)
        self.assertEqual((rc, out["status"]), (1, "failed"))
        self.assertIn("cloud down", out["why"])
        refusing = lambda *a, **k: {"is_error": True, "code": "FORBIDDEN"}
        with mock.patch.object(cards, "read_apps", lambda ws: APPS):
            rc, out = self.run_(["show", "apps"], task("show my apps"), execute=refusing)
        self.assertEqual((rc, out["status"]), (1, "failed"))
        self.assertIn("FORBIDDEN", out["why"])

    def test_a_card_that_cannot_be_recorded_is_posted_and_says_its_buttons_will_not_act(self):
        with mock.patch.object(cards, "read_apps", lambda ws: APPS), \
                mock.patch.object(cards.issued_cards, "record", side_effect=OSError("disk full")):
            rc, out = self.run_(["show", "apps"], task("show my apps"), execute=Fake().execute)
        self.assertEqual((rc, out["status"], out["event_id"]), (0, "posted", "$card"))
        self.assertIn("OSError", out["why"])

    def test_without_an_injected_core_or_sender_it_posts_as_the_core_s_own_agent(self):
        import pool_provision
        fake = Fake()
        path = self.ws / "task.txt"
        path.write_text(task("show my apps"))
        out = io.StringIO()
        with mock.patch.object(cards, "_core", lambda ws: CORE), \
                mock.patch.object(pool_provision, "core_actions", lambda ws, lane: (fake, CORE)), \
                mock.patch.object(cards, "read_apps", lambda ws: APPS), \
                mock.patch("workspace_default.resolve_workspace", lambda: str(self.ws)), \
                contextlib.redirect_stdout(out):
            rc = cards.main(["show", "apps", "--task-file", str(path)])
        self.assertEqual((rc, json.loads(out.getvalue())["status"]), (0, "posted"))
        self.assertEqual(fake.calls[0][3], DM)
        with mock.patch.object(cards, "_core", side_effect=RuntimeError("no core lane")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cards.main(["--workspace", str(self.ws), "show", "apps", "--task-file", str(path)])
        self.assertEqual((rc, json.loads(out.getvalue())["status"]), (2, "refused"))
        with mock.patch.object(pool_provision, "core_lane", lambda ws, lane: CORE):
            self.assertEqual(cards._core(self.ws), CORE)


class Reply(Base):
    """A click acts only as the owner's reply, in their DM, to a card this agent posted that offered it."""

    @staticmethod
    def text(command):
        return f"Do {command}"

    def issue(self, *commands, event="$card"):
        _, card = ac.build("test", "info", title="t", body="b",
                           buttons=[ac.button(c, "reply", reply=self.text(c), command=c) for c in commands])
        self.assertTrue(cards.issued_cards.record(self.ws, event, card))

    def reply(self, command, *, said=None, card_id="test", **kw):
        said = self.text(command) if said is None else said
        kw.setdefault("reply_to_event", "$card")
        kw.setdefault("card_click", json.dumps({"card_id": card_id, "action": said}))
        return self.run_(["reply"], task(said, **kw))

    def test_disconnect_acts_on_the_confirmed_variant_the_card_offered(self):
        done = []
        self.issue("card:connections:dev-s-space:disconnect:confirmed")
        with mock.patch.object(cards.connections, "read_rows", lambda ws=None: ROWS), \
                mock.patch.object(cards.connections, "disconnect", lambda inst, **kw: done.append(inst) or {}):
            rc, out = self.reply("card:connections:dev-s-space:disconnect")
            self.assertEqual((rc, out["status"], done), (1, "not_a_card_reply", []))
            rc, out = self.reply("card:connections:dev-s-space:disconnect:confirmed")
        self.assertEqual((rc, out["status"], done), (0, "done", ["dev-s-space"]))

    def test_reconnect_and_make_main_run_the_connections_units(self):
        calls = []
        self.issue("card:connections:dev-s-space:reconnect", "card:connections:dev-s-space:make-main",
                   "card:connections:s-space-w1:reconnect")
        with mock.patch.object(cards.connections, "read_rows", lambda ws=None: ROWS), \
                mock.patch.object(cards.connections, "reconnect", lambda inst, **kw: calls.append(("re", inst)) or {"pid": 7}), \
                mock.patch.object(cards.connections, "set_primary", lambda inst, **kw: calls.append(("main", inst)) or {"server": "dev.s.space", "relaunch_needed": True}):
            self.assertEqual(self.reply("card:connections:dev-s-space:reconnect"), (0, {"status": "done", "say": "Reconnected dev-s-space.", "pid": 7}))
            rc, out = self.reply("card:connections:dev-s-space:make-main")
            self.assertEqual(rc, 0)
            self.assertIn("takes over after a reconnect", out["say"])
            rc, out = self.reply("card:connections:s-space-w1:reconnect")
        self.assertEqual(calls, [("re", "dev-s-space"), ("main", "dev-s-space")])
        self.assertEqual((rc, out["status"]), (2, "refused"))

    def test_workers_pause_restart_and_a_confirmed_retire(self):
        import pool_manage
        calls = []
        said = ("card:workers:w1:pause", "card:workers:w1:restart", "card:workers:w1:retire:confirmed")
        self.issue(*said)
        with mock.patch.object(pool_manage, "stop", lambda ws, t, **k: calls.append(("stop", t)) or {"result": "stopped"}), \
                mock.patch.object(pool_manage, "reseat", lambda ws, repo, t, **k: {"result": "skipped"}), \
                mock.patch.object(pool_manage, "enable", lambda ws, repo, t, **k: calls.append(("enable", t)) or {"result": "failed"}), \
                mock.patch.object(pool_manage, "retire", lambda ws, t, **k: calls.append(("retire", t)) or {"result": "retired"}):
            got = [self.reply(x) for x in said + ("card:workers:w1:retire",)]
        self.assertEqual(calls, [("stop", "w1"), ("enable", "w1"), ("retire", "w1")])
        self.assertEqual([(rc, out["status"]) for rc, out in got],
                         [(0, "done"), (1, "failed"), (0, "done"), (1, "not_a_card_reply")])

    def test_restart_reseats_a_running_worker_and_enables_a_stopped_one(self):
        import pool_manage
        self.issue("card:workers:w1:restart")
        calls = []
        answers = iter([{"result": "reseated"}, {"result": "skipped", "why": "no seat running"},
                        {"result": "deferred", "owed": ["task-9"], "why": "it owes 1 task(s)"}])

        def reseat(ws, repo, t, **k):
            calls.append(("reseat", t))
            return next(answers)
        with mock.patch.object(pool_manage, "reseat", reseat), \
                mock.patch.object(pool_manage, "enable", lambda ws, repo, t, **k: calls.append(("enable", t)) or {"result": "enabled"}):
            got = [self.reply("card:workers:w1:restart")[1] for _ in range(3)]
        self.assertEqual(calls, [("reseat", "w1"), ("reseat", "w1"), ("enable", "w1"), ("reseat", "w1")])
        self.assertEqual([g["result"]["result"] for g in got], ["reseated", "enabled", "deferred"])

    def test_workers_or_servers_sharing_a_name_keep_working_controls(self):
        import pool_manage
        twins = [{"worker_id": "w1", "name": "Pylon", "state": "live", "paused": False, "session_alive": True},
                 {"worker_id": "w9", "name": "Pylon", "state": "live", "paused": False, "session_alive": True}]
        _, card = cards.workers_card(twins, {}, {})
        offers = cards.issued_cards.offered(card)
        self.assertEqual(offers["Pause Pylon (w9)"], "card:workers:w9:pause")
        self.assertEqual(offers["Pause Pylon (w1)"], "card:workers:w1:pause")
        self.assertTrue(cards.issued_cards.record(self.ws, "$twins", card))
        stopped = []
        with mock.patch.object(pool_manage, "stop", lambda ws, t, **k: stopped.append(t) or {"result": "stopped"}):
            rc, out = self.reply("", said="Pause Pylon (w9)", card_id="workers", reply_to_event="$twins")
        self.assertEqual((rc, out["status"], stopped), (0, "done", ["w9"]))
        rows = [{"instance": "a", "server": "s.space", "state": "connected", "primary": True},
                {"instance": "b", "server": "s.space", "state": "failed"}]
        offers = cards.issued_cards.offered(cards.connections_card(rows, [])[1])
        self.assertEqual(offers["Reconnect s.space (b)"], "card:connections:b:reconnect")

    def test_app_replies_never_disconnect(self):
        self.issue("card:apps:gmail:disconnect", "card:apps:gmail:switch", "card:apps:gmail:reconnect")
        rc, out = self.reply("card:apps:gmail:disconnect")
        self.assertEqual((rc, out["status"]), (0, "say"))
        self.assertIn("Settings → Integrations", out["say"])
        self.assertEqual(self.reply("card:apps:gmail:switch")[1],
                         {"status": "handoff", "skill": "connect-apps", "step": "switch", "slug": "gmail"})
        import connectors
        with mock.patch.object(connectors, "list_markers", lambda ws: []):
            self.assertEqual(self.reply("card:apps:gmail:reconnect")[1],
                             {"status": "handoff", "skill": "connect-apps", "step": "connect", "slug": "gmail"})

    def test_a_click_on_a_real_posted_card_round_trips(self):
        fake = Fake()
        with mock.patch.object(cards, "read_connections", lambda ws: (ROWS, BRIDGES)):
            rc, out = self.run_(["show", "connections"], task("show my connections"), execute=fake.execute)
        self.assertEqual(rc, 0)
        done = []
        with mock.patch.object(cards.connections, "read_rows", lambda ws=None: ROWS), \
                mock.patch.object(cards.connections, "set_primary", lambda inst, **kw: done.append(inst) or {}):
            rc, out = self.reply("", said="Make dev.s.space my main connection", card_id="connections",
                                 reply_to_event=out["event_id"])
        self.assertEqual((rc, out["status"], done), (0, "done", ["dev-s-space"]))

    def test_a_reply_to_a_card_this_agent_did_not_post_is_ignored(self):
        self.issue("card:workers:w1:pause")
        import pool_manage
        with mock.patch.object(pool_manage, "stop", mock.Mock(side_effect=AssertionError("acted"))):
            for reply_to in ("$forged-by-a-member", ""):
                rc, out = self.reply("card:workers:w1:pause", reply_to_event=reply_to)
                self.assertEqual((rc, out["status"]), (1, "not_a_card_reply"), reply_to)

    def test_a_payload_the_card_did_not_offer_is_ignored(self):
        self.issue("card:workers:w1:pause")
        import pool_manage
        with mock.patch.object(pool_manage, "retire", mock.Mock(side_effect=AssertionError("acted"))):
            for said in ("card:workers:w1:retire:confirmed", "card:workers:w2:pause", "card:circle:-:shuffle"):
                rc, out = self.reply(said)
                self.assertEqual((rc, out["status"]), (1, "not_a_card_reply"), said)

    def test_anyone_but_the_owner_in_their_dm_is_ignored(self):
        self.issue("card:workers:w1:pause")
        import pool_manage
        with mock.patch.object(pool_manage, "stop", mock.Mock(side_effect=AssertionError("acted"))):
            for kw in ({"tier": "team"}, {"user": "@x:s.space"}, {"room": ROOM}, {"collaborator": "true"}):
                rc, out = self.reply("card:workers:w1:pause", **kw)
                self.assertEqual((rc, out["status"]), (1, "not_a_card_reply"), kw)

    def test_other_words_and_other_cards_are_not_its_replies(self):
        self.assertEqual(self.run_(["reply"], task("show my workers")), (1, {"status": "not_a_card_reply"}))
        self.assertEqual(self.reply("card:worker-approval:w1:resend")[1]["status"], "not_a_card_reply")
        self.issue("card:workers:w1:pause")
        import pool_manage
        with mock.patch.object(pool_manage, "stop", mock.Mock(side_effect=AssertionError("acted"))):
            typed = self.run_(["reply"], task(self.text("card:workers:w1:pause"), reply_to_event="$card"))
            self.assertEqual(typed, (1, {"status": "not_a_card_reply"}))
            rc, out = self.reply("card:workers:w1:pause", card_id="another-card")
            self.assertEqual((rc, out["status"]), (1, "not_a_card_reply"))
            rc, out = self.reply("card:workers:w1:pause", said="Pause everything")
            self.assertEqual((rc, out["status"]), (1, "not_a_card_reply"))

    def test_a_core_that_cannot_be_read_or_a_handler_that_raises_is_named(self):
        self.issue("card:workers:w1:pause")
        path = self.ws / "task.txt"
        path.write_text(task(self.text("card:workers:w1:pause"), reply_to_event="$card", card_click=json.dumps(
            {"card_id": "test", "action": self.text("card:workers:w1:pause")})))
        out = io.StringIO()
        with mock.patch.object(cards, "_core", side_effect=RuntimeError("no core lane")), \
                contextlib.redirect_stdout(out):
            rc = cards.main(["--workspace", str(self.ws), "reply", "--task-file", str(path)])
        self.assertEqual((rc, json.loads(out.getvalue())["status"]), (2, "refused"))
        import pool_manage
        with mock.patch.object(pool_manage, "stop", side_effect=RuntimeError("tmux gone")):
            rc, got = self.reply("card:workers:w1:pause")
        self.assertEqual((rc, got), (1, {"status": "failed", "why": "RuntimeError: tmux gone"}))


class Units(Base):
    """Each reply unit refuses what no card offers, so a future card cannot reach an unnamed action."""

    def test_unknown_verbs_and_targets_are_refused(self):
        R = ac.Reply
        with mock.patch.object(cards.connections, "read_rows", lambda ws=None: ROWS):
            self.assertEqual(cards._connections_reply(self.ws, R("connections", "dev-s-space", "explode", False))["status"],
                             "refused")
            self.assertIn("(legacy)", cards._connections_reply(self.ws, R("connections", "", "reconnect", False))["why"])
        self.assertEqual(cards._workers_reply(self.ws, R("workers", "w1", "explode", False))["status"], "refused")
        self.assertEqual(cards._apps_reply(self.ws, R("apps", "Bad!", "switch", False), {})["status"], "refused")
        self.assertEqual(cards._apps_reply(self.ws, R("apps", "gmail", "explode", False), {})["status"], "refused")


if __name__ == "__main__":
    unittest.main()
