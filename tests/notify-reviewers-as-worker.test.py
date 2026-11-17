#!/usr/bin/env python3
"""notify_reviewers posts as a worker's own agent from that worker's session.

A worker with its own enabled agent asks in a room its agent and the reviewer's
agent share, through room_ops in its own session (which routes to the worker's
lane); with no such room it refuses rather than post as the core's agent. A core
session, or a worker without its own agent, keeps the core path unchanged.

Run: python3 tests/notify-reviewers-as-worker.test.py   (stdlib only, no network)
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from _helpers.hermetic_env import isolate_os_environ

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

SCRIPT = (Path(__file__).resolve().parent.parent / "skills"
          / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")

WORKER, WAGENT = "w1abcdef", "@worker-agent:x"
ROSTER = {"rui": {"stand": "@sutando-rui:x", "room": "!triage:x", "allowlisted": True},
          "kim": {"stand": "@sutando-kim:x", "room": "!triage:x", "allowlisted": True}}


def _load():
    spec = importlib.util.spec_from_file_location("_nr_as_worker", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.codeowner_gate = lambda *a, **k: (True, [])
    return mod


class FakeRoomOps:
    """Answers room_ops `members <room>`, `rooms` and `mention`; records every argv."""

    def __init__(self, members, joined=()):
        self.members, self.joined, self.calls = members, list(joined), []

    def __call__(self, argv, **kw):
        assert Path(argv[1]).name == "room_ops.py", argv
        self.calls.append(list(argv[2:]))
        cmd = argv[2]
        if cmd == "members":
            out = {"ok": True, "members": [{"user_id": u} for u in self.members.get(argv[3], [])]}
        elif cmd == "rooms":
            out = {"ok": True, "rooms": self.joined}
        else:
            out = {"ok": True, "event_id": "$e"}
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")

    def mentions(self):
        return [c for c in self.calls if c[0] == "mention"]


def fake_connections(worker="", row=None, routed=None):
    """A stand-in for src/connections.py: the session's worker, its identity row, and per-room routing."""
    m = types.ModuleType("connections")

    class ConnectionsError(Exception):
        pass
    m.ConnectionsError = ConnectionsError
    m.session_worker = lambda environ=None: worker
    m.worker_enabled_identity = lambda w, workspace=None: row if w == worker else None
    m.connection_for_room = lambda room, workspace=None, environ=None: (routed or {}).get(room)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        roster = Path(tmp.name) / "roster.json"
        roster.write_text(json.dumps(ROSTER))
        p = patch.dict(os.environ, {"SUTANDO_SCI_ROSTER": str(roster)}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        # Interaction ids are minted per ask; none is wanted here.
        self.mod._correlation = lambda: None

    def run_main(self, fake_ops, conns, *extra):
        argv = ["notify_reviewers.py", "--send", "--no-repo", "as-worker test",
                "--reviewers", "rui,kim", "--message", "please review", *extra]
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {"connections": conns}), \
                patch.object(self.mod.subprocess, "run", fake_ops), \
                patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.mod.main()
        return rc, out.getvalue(), err.getvalue()


class WorkerWithOwnAgent(Base):
    ROW = {"worker": WORKER, "agent_mxid": WAGENT, "instance": "s-space-w1abcdef"}

    def test_posts_as_the_worker_in_a_room_both_agents_share(self):
        # The recorded room routes as the core's agent; the worker's own room holds both.
        ops = FakeRoomOps({"!shared:x": [WAGENT, "@sutando-rui:x", "@sutando-kim:x"]},
                          joined=["!shared:x"])
        conns = fake_connections(WORKER, self.ROW, {"!shared:x": self.ROW, "!triage:x": {"worker": ""}})
        rc, out, err = self.run_main(ops, conns)
        self.assertEqual(rc, 0, err)
        sent = ops.mentions()
        self.assertEqual(len(sent), 2)
        for call in sent:
            self.assertEqual(call[3], "!shared:x")
            self.assertEqual(call[-2:], ["--agent", WAGENT])
        self.assertIn("asking as this worker's own agent", err)
        # Success names neither the worker's identity nor the room chosen.
        self.assertNotIn(WAGENT, err)
        self.assertNotIn("!shared:x", err)
        # The core-routed recorded room was never read as a venue.
        self.assertNotIn(["members", "!triage:x"], ops.calls)

    def test_no_shared_room_refuses_and_never_falls_back_to_the_core(self):
        ops = FakeRoomOps({"!shared:x": [WAGENT, "@other:x"]}, joined=["!shared:x"])
        conns = fake_connections(WORKER, self.ROW, {"!shared:x": self.ROW, "!triage:x": {"worker": ""}})
        rc, out, err = self.run_main(ops, conns)
        self.assertEqual(rc, 5, err)
        self.assertEqual(ops.mentions(), [])
        self.assertIn("NO SHARED ROOM", err)
        self.assertIn("never falls back to the core", err)
        # One generic line per reviewer: no candidate rooms, no agent ids.
        for detail in ("!shared:x", "!triage:x", WAGENT, "@sutando-rui:x", "@sutando-kim:x"):
            self.assertNotIn(detail, err)

    def test_room_flag_is_the_only_candidate(self):
        ops = FakeRoomOps({"!shared:x": [WAGENT, "@sutando-rui:x", "@sutando-kim:x"],
                           "!named:x": [WAGENT, "@sutando-rui:x"]}, joined=["!shared:x"])
        conns = fake_connections(WORKER, self.ROW, {"!shared:x": self.ROW, "!named:x": self.ROW})
        rc, out, err = self.run_main(ops, conns, "--room", "!named:x", "--allow-single", "test")
        self.assertEqual(rc, 5, err)
        self.assertEqual([c[3] for c in ops.mentions()], ["!named:x"])
        self.assertEqual(ops.mentions()[0][-2:], ["--agent", WAGENT])
        self.assertNotIn(["rooms"], ops.calls)

    def test_unreadable_membership_is_not_a_shared_room(self):
        ops = FakeRoomOps({}, joined=["!shared:x"])

        def run(argv, **kw):
            if argv[2] == "members":
                ops.calls.append(list(argv[2:]))
                return types.SimpleNamespace(returncode=0, stdout='{"ok": false}', stderr="")
            return FakeRoomOps.__call__(ops, argv, **kw)
        conns = fake_connections(WORKER, self.ROW, {"!shared:x": self.ROW, "!triage:x": self.ROW})
        rc, out, err = self.run_main(run, conns)
        self.assertEqual(rc, 5, err)
        self.assertEqual(ops.mentions(), [])


class CorePathUnchanged(Base):
    def _core_run(self, conns):
        ops = FakeRoomOps({"!triage:x": ["@sutando-rui:x", "@sutando-kim:x"]}, joined=["!other:x"])
        rc, out, err = self.run_main(ops, conns)
        self.assertEqual(rc, 0, err)
        sent = ops.mentions()
        self.assertEqual([c[3] for c in sent], ["!triage:x", "!triage:x"])
        for call in sent:
            self.assertNotIn("--agent", call)
        self.assertNotIn(["rooms"], ops.calls)
        self.assertNotIn("as worker", err)

    def test_core_session_posts_as_before(self):
        self._core_run(fake_connections(worker=""))

    def test_worker_without_its_own_agent_takes_the_core_path(self):
        self._core_run(fake_connections(worker=WORKER, row=None))

    def test_no_src_beside_the_script_takes_the_core_path(self):
        with patch.dict(sys.modules, {"connections": None}):
            self.assertEqual(self.mod.acting_worker(), ("", ""))


class Helpers(Base):
    def test_acting_worker_reads_the_session_and_its_enabled_identity(self):
        row = {"worker": WORKER, "agent_mxid": f" {WAGENT} "}
        with patch.dict(sys.modules, {"connections": fake_connections(WORKER, row)}):
            self.assertEqual(self.mod.acting_worker(), (WORKER, WAGENT))

    def test_routes_as_worker_only_when_connections_routes_to_that_worker(self):
        conns = fake_connections(WORKER, {}, {"!a:x": {"worker": WORKER}, "!b:x": {"worker": "other"}})

        def refuse(room, workspace=None, environ=None):
            raise conns.ConnectionsError("no connection")
        with patch.dict(sys.modules, {"connections": conns}):
            self.assertTrue(self.mod.routes_as_worker("!a:x", WORKER))
            self.assertFalse(self.mod.routes_as_worker("!b:x", WORKER))
            self.assertFalse(self.mod.routes_as_worker("!none:x", WORKER))
            conns.connection_for_room = refuse
            self.assertFalse(self.mod.routes_as_worker("!a:x", WORKER))

    def test_a_worker_agent_with_no_recorded_id_is_refused(self):
        room, why = self.mod.worker_room_for({"stand": "@s:x"}, WORKER, "", ["!a:x"])
        self.assertIsNone(room)
        self.assertIn("no recorded id", why)


if __name__ == "__main__":
    unittest.main()
