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
        replies = {i["reply"]: i for i in menu_items(card)}
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
        items = {i["reply"]: i for i in menu_items(card)}
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
        self.assertEqual(rows[1]["actions"][0]["reply"], "card:apps:linear:reconnect")
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


class Reply(Base):
    def test_disconnect_acts_only_on_the_confirmed_variant(self):
        done = []
        with mock.patch.object(cards.connections, "read_rows", lambda ws=None: ROWS), \
                mock.patch.object(cards.connections, "disconnect", lambda inst, **kw: done.append(inst) or {}):
            rc, out = self.run_(["reply"], task("card:connections:dev-s-space:disconnect"))
            self.assertEqual((rc, out["status"], done), (1, "needs_confirm", []))
            rc, out = self.run_(["reply"], task("card:connections:dev-s-space:disconnect:confirmed"))
        self.assertEqual((rc, out["status"], done), (0, "done", ["dev-s-space"]))

    def test_reconnect_and_make_main_run_the_connections_units(self):
        calls = []
        with mock.patch.object(cards.connections, "read_rows", lambda ws=None: ROWS), \
                mock.patch.object(cards.connections, "reconnect", lambda inst, **kw: calls.append(("re", inst)) or {"pid": 7}), \
                mock.patch.object(cards.connections, "set_primary", lambda inst, **kw: calls.append(("main", inst)) or {"server": "dev.s.space"}):
            self.assertEqual(self.run_(["reply"], task("card:connections:dev-s-space:reconnect"))[0], 0)
            self.assertEqual(self.run_(["reply"], task("card:connections:dev-s-space:make-main"))[0], 0)
            rc, out = self.run_(["reply"], task("card:connections:s-space-w1:reconnect"))
        self.assertEqual(calls, [("re", "dev-s-space"), ("main", "dev-s-space")])
        self.assertEqual((rc, out["status"]), (2, "refused"))

    def test_workers_pause_restart_and_a_confirmed_retire(self):
        import pool_manage
        calls = []
        with mock.patch.object(pool_manage, "stop", lambda ws, t, **k: calls.append(("stop", t)) or {"result": "stopped"}), \
                mock.patch.object(pool_manage, "enable", lambda ws, repo, t, **k: calls.append(("enable", t)) or {"result": "enabled"}), \
                mock.patch.object(pool_manage, "retire", lambda ws, t, **k: calls.append(("retire", t)) or {"result": "retired"}):
            for said in ("card:workers:w1:pause", "card:workers:w1:restart", "card:workers:w1:retire",
                         "card:workers:w1:retire:confirmed"):
                self.run_(["reply"], task(said))
        self.assertEqual(calls, [("stop", "w1"), ("enable", "w1"), ("retire", "w1")])

    def test_app_replies_never_disconnect(self):
        rc, out = self.run_(["reply"], task("card:apps:gmail:disconnect"))
        self.assertEqual((rc, out["status"]), (0, "say"))
        self.assertIn("Settings → Integrations", out["say"])
        rc, out = self.run_(["reply"], task("card:apps:gmail:switch"))
        self.assertEqual(out, {"status": "handoff", "skill": "connect-apps", "step": "switch", "slug": "gmail"})

    def test_only_the_owner_in_their_dm(self):
        import pool_manage
        with mock.patch.object(pool_manage, "stop", mock.Mock(side_effect=AssertionError("acted"))):
            for text in (task("card:workers:w1:pause", tier="team"), task("card:workers:w1:pause", user="@x:s.space"),
                         task("card:workers:w1:pause", room=ROOM)):
                rc, out = self.run_(["reply"], text)
                self.assertEqual((rc, out["status"]), (2, "refused"))

    def test_other_words_and_other_cards_are_not_its_replies(self):
        self.assertEqual(self.run_(["reply"], task("show my workers")), (1, {"status": "not_a_card_reply"}))
        self.assertEqual(self.run_(["reply"], task("card:worker-approval:w1:resend"))[1]["status"], "not_a_card_reply")


if __name__ == "__main__":
    unittest.main()
