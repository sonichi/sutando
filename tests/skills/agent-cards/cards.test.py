#!/usr/bin/env python3
"""The owner's "show my …" cards (skills/agent-cards/scripts/cards.py) and their reply handlers.

- Each builder's card passes the client's checks (agent_card.problem): connections (core rows only,
  workers' own agents as a footer, chat-app bridges), workers, apps, and circle with an honest empty state.
- Cards follow the client's contract: a status pill, no shape on a member's avatar, apps split into
  Connected and Not connected with a `connect` action per unconnected app, "+ Connect an app" as a picker.
- `show` posts only for the owner, only in their checked DM, through the injected Action call, records the
  room, connection and handler, and `show apps` starts every connect waiter that is not running.
- `reply` acts only on the owner's click on a card this agent posted (agent_card.click_verdict); any other
  click prints the fixed refusal and nothing acts. Disconnect and Retire never act on the first click: it
  posts a confirm card, whose "Yes" acts once.

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
    {"instance": "s_dspace", "server": "s.space", "primary": True, "state": "connected", "owner_mxid": OWNER,
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
        self.assertNotIn("confirm", replies["card:connections:dev-s-space:disconnect"])
        self.assertFalse(any(c.endswith(":confirmed") for c in replies), "only a confirm card offers a confirmed command")
        self.assertEqual(card["header"]["status"], {"label": "2 of 3 connected"})
        self.assertEqual([r["avatar"] for r in servers["rows"]],
                         [{"mxid": "@owner-mini.agent:s.space"}, {"mxid": "@owner-mini.agent:dev.s.space"}, {"seed": "ag2.space"}])
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
        self.assertEqual(items["card:workers:w1:retire"]["reply"], "Retire Pylon")
        self.assertNotIn("card:workers:w1:retire:confirmed", items)
        self.assertEqual(rows[0]["avatar"], {"mxid": "@owner-pylon.agent:s.space"})
        self.assertEqual(card["buttons"][0]["reply"], "create a worker")

    def test_apps(self):
        pending = [{"slug": "notion", "name": "Notion"}, {"slug": "gmail", "name": "Gmail"},
                   {"slug": "linear", "name": "Linear"}, {"slug": "Bad Slug"}]
        body, card = cards.apps_card(APPS, pending)
        self.assertIsNone(ac.problem(card))
        on, off = card["groups"]
        self.assertEqual((on["label"], off["label"]), ("Connected", "Not connected"))
        self.assertEqual([(r["title"], r["subtitle"], r["avatar"]) for r in on["rows"]],
                         [("Gmail", "me@work.io · default", {"app": "gmail", "shape": "square"})])
        self.assertEqual(on["rows"][0]["actions"], [{"label": "Switch account", "kind": "reply",
                                                     "reply": "Switch my Gmail account", "command": "card:apps:gmail:switch"}])
        self.assertEqual([(r["title"], r["subtitle"], r["state"], r["actions"]) for r in off["rows"]], [
            ("Linear", "Signed out", "warn", [{"label": "Reconnect", "kind": "connect", "toolkit": "linear"}]),
            ("Notion", "Needed for a request you made", "warn", [{"label": "Connect", "kind": "connect", "toolkit": "notion"}])])
        self.assertEqual(card["buttons"], [{"label": "+ Connect an app", "kind": "connect_picker", "style": "secondary"}])
        self.assertEqual(card["header"]["status"], {"label": "1 connected"})
        self.assertEqual(body, "Your apps: 1 connected, 2 not connected. Manage them in Settings → Integrations.")
        self.assertEqual(cards.issued_cards.offered(card), {"Switch my Gmail account": "card:apps:gmail:switch"})
        self.assertTrue(cards.issued_cards.has_picker(card))
        _, only_off = cards.apps_card([APPS[1]])
        self.assertEqual([g["label"] for g in only_off["groups"]], ["Not connected"])
        self.assertNotIn("status", only_off["header"])

    def test_no_card_types_a_glyph_or_asks_for_a_reply_where_the_client_can_act(self):
        built = [cards.connections_card(ROWS, BRIDGES), cards.workers_card(WORKERS, {}, {}), cards.apps_card(APPS),
                 cards.circle_card([{"entity_id": "@ana:s.space", "kind": "human"}]),
                 cards.confirm_card(ac.Reply("workers", "w1", "retire", False), "Retire Pylon", now=1000.0)]
        for body, card in built:
            self.assertNotRegex(body + json.dumps(card, ensure_ascii=False), "[\u2713\u2714\u2705\u274c\u26a0]")
        plain = sorted(t for _, card in built for t, c in cards.issued_cards.offered(card).items() if not c)
        self.assertEqual(plain, ["create a worker", "show my workers"])

    def test_circle_and_its_empty_state(self):
        _, empty = cards.circle_card([])
        self.assertEqual(empty["text"], "I haven't recorded who I work with yet.")
        self.assertIsNone(ac.problem(empty))
        _, card = cards.circle_card([{"entity_id": "@ana:s.space", "kind": "human", "one_line": "reviews"},
                                     {"entity_id": "@ana-bot.agent:s.space", "kind": "agent"},
                                     {"entity_id": "svc", "kind": "service"}])
        self.assertIsNone(ac.problem(card))
        people, agents = card["groups"]
        self.assertEqual(people["rows"][0]["avatar"], {"mxid": "@ana:s.space"})
        self.assertEqual(agents["rows"][0]["avatar"], {"mxid": "@ana-bot.agent:s.space"})

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
        for name, fake in (("owner_dm", lambda row, ws=None: DM), ("read_rows", lambda ws=None: ROWS)):
            p = mock.patch.object(cards.connections, name, fake)
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
        entry = cards.issued_cards.live(self.ws, "$card")
        self.assertEqual((entry["room"], entry["lane"], entry["handler"]), (DM, "s_dspace", cards.HANDLER))

    def test_show_apps_starts_the_waiter_of_every_request_that_waits_for_an_app(self):
        import connectors
        markers = [{"wait_id": "w-alive", "toolkits": [{"slug": "notion", "name": "Notion"}]},
                   {"wait_id": "w-dead", "toolkits": [{"slug": "linear", "name": "Linear"}, "odd"]},
                   {"wait_id": "w-stuck", "toolkits": [{"slug": "asana", "name": "Asana"}]}]
        spawned, fake = [], Fake()

        def spawn(ws, wait_id):
            spawned.append(wait_id)
            if wait_id == "w-stuck":
                raise OSError("no exec")
            return 4242
        with mock.patch.object(connectors, "list_markers", lambda ws: markers), \
                mock.patch.object(connectors, "waiter_alive", lambda ws, wid: wid == "w-alive"), \
                mock.patch.object(connectors.Cloud, "connection_rows", lambda self, cached=False: APPS), \
                contextlib.redirect_stderr(io.StringIO()):
            rc, out = self.run_(["show", "apps"], task("show my apps"), execute=fake.execute, spawn=spawn)
            self.assertEqual((rc, out), (0, {"status": "posted", "event_id": "$card", "waits": ["w-alive", "w-dead"]}))
            self.assertEqual(spawned, ["w-dead", "w-stuck"])
            off = fake.calls[0][1]["extra_content"]["space.ag2.card"]["groups"][1]["rows"]
            self.assertEqual([(r["title"], r["actions"][0]["kind"]) for r in off],
                             [("Linear", "connect"), ("Notion", "connect"), ("Asana", "connect")])
            rc, out = self.run_(["show", "apps", "--print"], task("show my apps"), spawn=spawn)
        self.assertEqual((rc, len(spawned)), (0, 2), "a preview starts nothing")
        with mock.patch.object(connectors, "list_markers", lambda ws: []), \
                mock.patch.object(connectors.Cloud, "connection_rows", lambda self, cached=False: APPS):
            rc, out = self.run_(["show", "apps"], task("show my apps"), execute=fake.execute)
        self.assertEqual(out, {"status": "posted", "event_id": "$card"})

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
        with mock.patch.object(connectors.Cloud, "connection_rows", lambda self, cached=False: APPS), \
                mock.patch.object(connectors, "list_markers", lambda ws: [
                    {"wait_id": "w1", "toolkits": [{"slug": "notion", "name": "Notion"}]}]):
            self.assertEqual(cards.read_apps(self.ws), (APPS, [{"slug": "notion", "name": "Notion"}]))
            self.assertEqual(cards.build_kind("apps", self.ws)[1]["id"], "apps")

    def test_print_shows_the_card_without_posting_it(self):
        with mock.patch.object(cards, "read_apps", lambda ws: (APPS, [])):
            rc, out = self.run_(["show", "apps", "--print"], task("show my apps"))
        self.assertEqual((rc, out["card"]["id"]), (0, "apps"))
        self.assertFalse(cards.issued_cards.ledger_path(self.ws).exists())

    def test_a_source_or_a_send_that_fails_is_named(self):
        with mock.patch.object(cards, "read_apps", side_effect=RuntimeError("cloud down")):
            rc, out = self.run_(["show", "apps"], task("show my apps"), execute=Fake().execute)
        self.assertEqual((rc, out["status"]), (1, "failed"))
        self.assertIn("cloud down", out["why"])
        refusing = lambda *a, **k: {"is_error": True, "code": "FORBIDDEN"}
        with mock.patch.object(cards, "read_apps", lambda ws: (APPS, [])):
            rc, out = self.run_(["show", "apps"], task("show my apps"), execute=refusing)
        self.assertEqual((rc, out["status"]), (1, "failed"))
        self.assertIn("FORBIDDEN", out["why"])

    def test_a_card_that_cannot_be_recorded_is_posted_and_says_its_buttons_will_not_act(self):
        with mock.patch.object(cards, "read_apps", lambda ws: (APPS, [])), \
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
                mock.patch.object(cards, "read_apps", lambda ws: (APPS, [])), \
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
    """A click acts only as the owner's click, in the room of a card this agent posted, on a text it offered."""

    @staticmethod
    def text(command):
        return f"Do {command}"

    def issue(self, *commands, event="$card", lane=""):
        _, card = ac.build("test", "info", title="t", body="b",
                           buttons=[ac.button(c, "reply", reply=self.text(c), command=c) for c in commands])
        self.assertTrue(cards.issued_cards.record(self.ws, event, card, room=DM, lane=lane, handler=cards.HANDLER))

    def reply(self, command, *, said=None, card_id="test", execute=None, **kw):
        said = self.text(command) if said is None else said
        kw.setdefault("reply_to_event", "$card")
        kw.setdefault("card_click", json.dumps({"card_id": card_id, "action": said}))
        return self.run_(["reply"], task(said, **kw), **({"execute": execute} if execute else {}))

    def forged(self, *a, **kw):
        rc, out = self.reply(*a, **kw)
        self.assertEqual((rc, out["status"], out["say"]), (1, "forged_or_stale_click", ac.UNMATCHED_CLICK), (a, kw))

    def first_click(self, kind, target, verb, words):
        """Show a card that offers the destructive action, click it, and return the confirm card posted."""
        _, card = ac.build(kind, "info", title="t", body="b", buttons=[
            ac.button(words, "reply", reply=words, command=ac.reply(kind, target, verb))])
        cards.issued_cards.record(self.ws, "$list", card, room=DM, handler=cards.HANDLER)
        fake = Fake()
        fake.execute = lambda action, arguments, **kw: fake.calls.append((action, arguments, kw)) or {
            "result": {"event_id": "$confirm"}}
        rc, out = self.reply("", said=words, card_id=kind, reply_to_event="$list", execute=fake.execute)
        self.assertEqual((rc, out), (0, {"status": "asked", "event_id": "$confirm"}))
        return fake.calls[0][1]["extra_content"]["space.ag2.card"], fake.calls[0]

    def test_the_first_click_on_disconnect_only_asks_and_the_confirm_card_s_yes_acts_once(self):
        done = []
        with mock.patch.object(cards.connections, "disconnect", lambda inst, **kw: done.append(inst) or {}):
            confirm, call = self.first_click("connections", "dev-s-space", "disconnect", "Disconnect dev.s.space")
            self.assertEqual(done, [], "the first click never disconnects")
            self.assertIsNone(ac.problem(confirm))
            self.assertEqual((confirm["id"], confirm["state"], confirm["header"]["title"]),
                             ("confirm:connections:dev-s-space:disconnect", "pending", "Disconnect dev.s.space?"))
            self.assertEqual([(b["label"], b["reply"], b["command"]) for b in confirm["buttons"]], [
                ("Yes, disconnect dev.s.space", "Yes, disconnect dev.s.space", "card:connections:dev-s-space:disconnect:confirmed"),
                ("Keep dev.s.space", "Keep dev.s.space", "card:connections:dev-s-space:keep")])
            self.assertEqual((call[1]["reply_to"], call[2]["room_id"], call[2]["operation_id"]), ("$ask", DM, "task-1:confirm-card"))
            entry = cards.issued_cards.live(self.ws, "$confirm")
            self.assertLessEqual(entry["expires_at"] - cards.time.time(), cards.CONFIRM_TTL_S)
            yes = dict(said="Yes, disconnect dev.s.space", card_id=confirm["id"], reply_to_event="$confirm")
            self.forged("", said="Yes, disconnect dev.s.space", card_id="connections", reply_to_event="$list")
            rc, out = self.reply("", **yes)
            self.assertEqual((rc, out["status"], done), (0, "done", ["dev-s-space"]))
            self.forged("", **yes)
        self.assertEqual(done, ["dev-s-space"], "a confirm card answers once")

    def test_retire_asks_first_and_keep_drops_the_question(self):
        import pool_manage
        retired = []
        with mock.patch.object(pool_manage, "retire", lambda ws, t, **k: retired.append(t) or {"result": "retired"}):
            confirm, _ = self.first_click("workers", "w1", "retire", "Retire Pylon")
            self.assertIn("loses its own agent", confirm["text"])
            keep = dict(said="Keep Pylon", card_id=confirm["id"], reply_to_event="$confirm")
            self.assertEqual(self.reply("", **keep), (0, {"status": "say", "say": "Okay, I left it as it is."}))
            self.forged("", said="Yes, retire Pylon", card_id=confirm["id"], reply_to_event="$confirm")
            self.assertEqual(retired, [])
            confirm, _ = self.first_click("workers", "w1", "retire", "Retire Pylon")
            rc, out = self.reply("", said="Yes, retire Pylon", card_id=confirm["id"], reply_to_event="$confirm")
        self.assertEqual((rc, out["status"], retired), (0, "done", ["w1"]))

    def test_a_confirm_card_that_cannot_be_posted_or_recorded_does_nothing(self):
        import pool_manage
        import pool_provision
        _, card = ac.build("workers", "info", title="t", body="b", buttons=[
            ac.button("Retire Pylon", "reply", reply="Retire Pylon", command="card:workers:w1:retire")])
        cards.issued_cards.record(self.ws, "$list", card, room=DM, lane="s_dspace", handler=cards.HANDLER)
        click = dict(said="Retire Pylon", card_id="workers", reply_to_event="$list")
        refusing = mock.Mock(execute=lambda *a, **k: {"is_error": True, "code": "FORBIDDEN"})
        lanes = []
        with mock.patch.object(pool_manage, "retire", mock.Mock(side_effect=AssertionError("acted"))), \
                mock.patch.object(pool_provision, "core_actions", lambda ws, lane: lanes.append(lane) or (refusing, CORE)):
            rc, out = self.reply("", **click)
            self.assertEqual((rc, out["status"], lanes), (1, "failed", ["s_dspace"]))
            self.assertIn("FORBIDDEN", out["why"])
            real = cards.issued_cards.record
            with mock.patch.object(cards.issued_cards, "record", side_effect=OSError("disk full")):
                rc, out = self.reply("", execute=Fake().execute, **click)
            self.assertEqual((rc, out["status"]), (1, "failed"))
            self.assertIn("will not act", out["why"])
            self.assertIs(cards.issued_cards.record, real)
            long = "Retire " + "x" * 120
            _, card = ac.build("workers", "info", title="t", body="b", buttons=[
                ac.button("Retire", "reply", reply=long, command="card:workers:w1:retire")])
            cards.issued_cards.record(self.ws, "$long", card, room=DM, handler=cards.HANDLER)
            rc, out = self.reply("", said=long, card_id="workers", reply_to_event="$long", execute=Fake().execute)
            self.assertEqual((rc, out["status"]), (0, "asked"), "a long name is cut, never refused")

    def test_reconnect_and_make_main_run_the_connections_units(self):
        calls = []
        self.issue("card:connections:dev-s-space:reconnect", "card:connections:dev-s-space:make-main",
                   "card:connections:s-space-w1:reconnect")
        with mock.patch.object(cards.connections, "reconnect", lambda inst, **kw: calls.append(("re", inst)) or {"pid": 7}), \
                mock.patch.object(cards.connections, "set_primary", lambda inst, **kw: calls.append(("main", inst)) or {"server": "dev.s.space", "relaunch_needed": True}):
            self.assertEqual(self.reply("card:connections:dev-s-space:reconnect"), (0, {"status": "done", "say": "Reconnected dev-s-space.", "pid": 7}))
            rc, out = self.reply("card:connections:dev-s-space:make-main")
            self.assertEqual(rc, 0)
            self.assertIn("takes over after a reconnect", out["say"])
            rc, out = self.reply("card:connections:s-space-w1:reconnect")
        self.assertEqual(calls, [("re", "dev-s-space"), ("main", "dev-s-space")])
        self.assertEqual((rc, out["status"]), (2, "refused"))

    def test_workers_pause_and_restart(self):
        import pool_manage
        calls = []
        said = ("card:workers:w1:pause", "card:workers:w1:restart")
        self.issue(*said)
        with mock.patch.object(pool_manage, "stop", lambda ws, t, **k: calls.append(("stop", t)) or {"result": "stopped"}), \
                mock.patch.object(pool_manage, "reseat", lambda ws, repo, t, **k: {"result": "skipped"}), \
                mock.patch.object(pool_manage, "enable", lambda ws, repo, t, **k: calls.append(("enable", t)) or {"result": "failed"}):
            got = [self.reply(x) for x in said]
        self.assertEqual(calls, [("stop", "w1"), ("enable", "w1")])
        self.assertEqual([(rc, out["status"]) for rc, out in got], [(0, "done"), (1, "failed")])

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
        self.assertTrue(cards.issued_cards.record(self.ws, "$twins", card, room=DM))
        stopped = []
        with mock.patch.object(pool_manage, "stop", lambda ws, t, **k: stopped.append(t) or {"result": "stopped"}):
            rc, out = self.reply("", said="Pause Pylon (w9)", card_id="workers", reply_to_event="$twins")
        self.assertEqual((rc, out["status"], stopped), (0, "done", ["w9"]))
        rows = [{"instance": "a", "server": "s.space", "state": "connected", "primary": True},
                {"instance": "b", "server": "s.space", "state": "failed"}]
        offers = cards.issued_cards.offered(cards.connections_card(rows, [])[1])
        self.assertEqual(offers["Reconnect s.space (b)"], "card:connections:b:reconnect")

    def test_switch_account_hands_over_to_connect_apps(self):
        self.issue("card:apps:gmail:switch")
        self.assertEqual(self.reply("card:apps:gmail:switch"),
                         (0, {"status": "handoff", "skill": "connect-apps", "step": "switch", "slug": "gmail"}))

    def test_a_click_on_a_real_posted_card_round_trips_for_the_owner_of_its_connection(self):
        fake = Fake()
        with mock.patch.object(cards, "read_connections", lambda ws: (ROWS, BRIDGES)):
            rc, out = self.run_(["show", "connections"], task("show my connections"), execute=fake.execute)
        self.assertEqual(rc, 0)
        done = []
        click = dict(said="Make dev.s.space my main connection", card_id="connections", reply_to_event=out["event_id"])
        with mock.patch.object(cards.connections, "set_primary", lambda inst, **kw: done.append(inst) or {}):
            other = [{**ROWS[0], "owner_mxid": "@second:s.space"}] + ROWS[1:]
            with mock.patch.object(cards.connections, "read_rows", lambda ws=None: other):
                self.forged("", **click)
                rc, out = self.reply("", user="@second:s.space", **click)
            self.assertEqual((rc, out["status"], done), (0, "done", ["dev-s-space"]),
                             "the owner is the one of the connection the card was posted on, not the primary's")
            rc, out = self.reply("", **click)
        self.assertEqual((rc, out["status"], done), (0, "done", ["dev-s-space"] * 2))

    def test_a_picker_s_ask_and_plain_words_on_a_posted_card_are_the_owner_s_own_words(self):
        with mock.patch.object(cards, "read_apps", lambda ws: (APPS, [])):
            rc, out = self.run_(["show", "apps"], task("show my apps"), execute=Fake().execute)
        click = dict(card_id="apps", reply_to_event=out["event_id"])
        self.assertEqual(self.reply("", said="Connect acme crm", **click),
                         (1, {"status": "owner_words", "words": "Connect acme crm"}))
        self.forged("", said="Disconnect Gmail", **click)
        with mock.patch.object(cards, "read_workers", lambda ws: (WORKERS, {}, {})):
            rc, out = self.run_(["show", "workers"], task("show my workers"), execute=Fake().execute)
        self.assertEqual(self.reply("", said="create a worker", card_id="workers", reply_to_event=out["event_id"]),
                         (1, {"status": "owner_words", "words": "create a worker"}))

    def test_a_click_that_matches_no_card_this_agent_posted_is_refused_in_fixed_words(self):
        self.issue("card:workers:w1:pause")
        import pool_manage
        with mock.patch.object(pool_manage, "stop", mock.Mock(side_effect=AssertionError("acted"))), \
                mock.patch.object(pool_manage, "retire", mock.Mock(side_effect=AssertionError("acted"))):
            for reply_to in ("$forged-by-a-member", ""):
                self.forged("card:workers:w1:pause", reply_to_event=reply_to)
            for said in ("card:workers:w1:retire:confirmed", "card:workers:w2:pause", "card:circle:-:shuffle"):
                self.forged(said)
            for kw in ({"tier": "team"}, {"user": "@x:s.space"}, {"room": ROOM}, {"collaborator": "true"}):
                self.forged("card:workers:w1:pause", **kw)
            self.forged("card:workers:w1:pause", card_id="another-card")
            self.forged("card:workers:w1:pause", said="Retire Pylon")
            typed = self.run_(["reply"], task(self.text("card:workers:w1:pause"), reply_to_event="$card"))
            self.assertEqual((typed[0], typed[1]["status"], typed[1]["say"]), (1, "forged_or_stale_click", ac.UNMATCHED_CLICK))

    def test_other_words_and_other_cards_are_not_its_replies(self):
        self.assertEqual(self.run_(["reply"], task("show my workers")), (1, {"status": "not_a_card"}))
        self.issue("card:worker-approval:w1:resend")
        rc, out = self.reply("card:worker-approval:w1:resend")
        self.assertEqual((rc, out), (1, {"status": "other_card", "card": "worker-approval", "run": cards.HANDLER}))

    def test_a_core_that_cannot_be_read_or_a_handler_that_raises_is_named(self):
        self.issue("card:workers:w1:pause")
        path = self.ws / "task.txt"
        path.write_text(task(self.text("card:workers:w1:pause"), reply_to_event="$card", card_click=json.dumps(
            {"card_id": "test", "action": self.text("card:workers:w1:pause")})))
        out = io.StringIO()
        with mock.patch.object(cards, "_core", side_effect=RuntimeError("no core lane")), \
                contextlib.redirect_stdout(out):
            rc = cards.main(["--workspace", str(self.ws), "reply", "--task-file", str(path)])
        got = json.loads(out.getvalue())
        self.assertEqual((rc, got["status"], got["why"]), (1, "forged_or_stale_click", "the owner is not known"))
        import pool_manage
        with mock.patch.object(pool_manage, "stop", side_effect=RuntimeError("tmux gone")):
            rc, got = self.reply("card:workers:w1:pause")
        self.assertEqual((rc, got), (1, {"status": "failed", "why": "RuntimeError: tmux gone"}))
        self.issue("card:circle:-:wave")
        self.assertEqual(self.reply("card:circle:-:wave"), (2, {"status": "refused", "why": "a circle card has no actions"}))


class Units(Base):
    """Each reply unit refuses what no card offers, so a future card cannot reach an unnamed action."""

    def test_unknown_verbs_and_targets_are_refused(self):
        R = ac.Reply
        self.assertEqual(cards._connections_reply(self.ws, R("connections", "dev-s-space", "explode", False))["status"],
                         "refused")
        self.assertIn("(legacy)", cards._connections_reply(self.ws, R("connections", "", "reconnect", False))["why"])
        self.assertEqual(cards._workers_reply(self.ws, R("workers", "w1", "explode", False))["status"], "refused")
        self.assertEqual(cards._apps_reply(self.ws, R("apps", "Bad!", "switch", False))["status"], "refused")
        self.assertEqual(cards._apps_reply(self.ws, R("apps", "gmail", "explode", False))["status"], "refused")


if __name__ == "__main__":
    unittest.main()
