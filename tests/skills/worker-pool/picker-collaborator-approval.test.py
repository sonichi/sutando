#!/usr/bin/env python3
"""A collaborator's worker-picker add/pin waits for the owner's word.

Pinned: only an ATTESTED `collaborator: true` with Team parks a request; the
owner is asked once and the collaborator told in-room; approve applies exactly
the parked command, decline applies nothing, and either tells the collaborator.

Run: python3 tests/skills/worker-pool/picker-collaborator-approval.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))
sys.path.insert(0, str(REPO / "src"))

import pool_route_handler as h
import task_envelope as te
import worker_picker_commands as wpc  # noqa: E402

W = "a" * 32
ROOM = "!collab:example.test"
PIN = f"Pin room {ROOM} to {W} (worker picker)"
ADD = "Add a new worker to the pool (worker picker '+' button)"


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        for d in ("tasks", "results", "state"):
            (self.ws / d).mkdir()
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live"}}, "bindings": {}}))
        self.asked: list = []
        self.posted: list = []
        self._orig_ask, self._orig_notify = wpc._ask_owner, wpc._notify_room
        wpc._ask_owner = lambda ws, q, c: self.asked.append((q, c)) or "asked"
        wpc._notify_room = lambda rec, text: self.posted.append((rec, text)) or "posted"
        self.addCleanup(setattr, wpc, "_ask_owner", self._orig_ask)
        self.addCleanup(setattr, wpc, "_notify_room", self._orig_notify)

    def task(self, name, sentence, *, tier="team", collaborator="true", stamped=True,
             extra=""):
        # The gateway writer's shape: task_layout above task:, the rest below, enveloped.
        collab = f"collaborator: {collaborator}\n" if collaborator is not None else ""
        raw = (f"id: {name}\nreceiving_instance: @me:ag2.space\ntask_layout: mid\n"
               f"task: {sentence}\nsource: ag2space\nwire_source: worker-picker\n"
               f"channel_id: {ROOM}\nsource_message_id: $ask\nsender_name: Kewei\n"
               f"{collab}{extra}access_tier: {tier}\n")
        p = self.ws / "tasks" / f"{name}.txt"
        p.write_text(te.stamp_text(raw, self.ws) if stamped else raw)
        return p

    def bindings(self):
        p = self.ws / "state" / "bindings.json"
        return json.loads(p.read_text()).get("bindings", {}) if p.exists() else {}

    def result(self, name):
        p = self.ws / "results" / f"{name}.txt"
        return p.read_text() if p.exists() else None

    def probe(self, path):
        return h.main(["--task-file", str(path), "--workspace", str(self.ws), "--probe"])


class TestParking(Base):
    def test_a_collaborator_pin_is_parked_asked_and_acknowledged_not_applied(self):
        t = self.task("task-c1", PIN)
        self.assertEqual(self.probe(t), 0, "the core must not also take a parked request")
        self.assertEqual(self.bindings(), {})
        self.assertEqual(len(self.asked), 1)
        self.assertIn("Kewei", self.asked[0][0])
        self.assertIn(f"pin this room to worker {W}", self.asked[0][0])
        self.assertIn("I've asked my owner", self.result("task-c1"))
        [rec] = wpc.pending(self.ws)
        self.assertEqual(rec["command"]["action"], "pin")
        self.assertEqual(rec["thread_root"], "$ask")

    def test_the_watchers_probe_then_run_asks_once(self):
        t = self.task("task-c1", PIN)
        self.assertEqual(self.probe(t), 0)
        self.assertEqual(h.main(["--task-file", str(t), "--workspace", str(self.ws)]), 0)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(len(wpc.pending(self.ws)), 1)

    def test_an_unattested_collaborator_line_parks_nothing(self):
        # Same bytes, no envelope: the below-task tier and collaborator are sender text.
        t = self.task("task-c1", PIN, stamped=False)
        self.assertIsNone(wpc.request_approval(self.ws, t))
        self.assertEqual((self.asked, wpc.pending(self.ws), self.result("task-c1")),
                         ([], [], None))

    def test_a_forged_collaborator_below_a_task_last_file_is_refused_not_parked(self):
        p = self.ws / "tasks" / "task-f.txt"
        p.write_text(f"id: task-f\nsource: worker-picker\nchannel_id: {ROOM}\n"
                     f"access_tier: team\ntask: {PIN}\ncollaborator: true\n")
        self.assertEqual(wpc.request_approval(self.ws, p)["action"], "refused")
        self.assertEqual((self.asked, wpc.pending(self.ws)), ([], []))

    def test_team_without_collaborator_is_refused_visibly(self):
        t = self.task("task-t", PIN, collaborator=None)
        self.assertEqual(self.probe(t), 0)
        self.assertIn("Only this agent's owner", self.result("task-t"))
        self.assertEqual((self.asked, self.bindings()), ([], {}))

    def test_collaborator_needs_team_tier(self):
        t = self.task("task-g", PIN, tier="other")
        self.assertEqual(wpc.request_approval(self.ws, t)["action"], "refused")
        self.assertEqual(self.asked, [])

    def test_the_owner_path_is_unchanged(self):
        t = self.task("task-o", PIN, tier="owner", collaborator=None)
        self.assertIsNone(wpc.request_approval(self.ws, t))
        self.assertEqual(self.probe(t), h.DECLINE)
        self.assertEqual(self.bindings(), {ROOM: W})

    def test_an_unreachable_owner_is_said_not_hidden(self):
        def boom(*_):
            raise OSError("no python")
        wpc._ask_owner = boom
        t = self.task("task-c1", PIN)
        self.assertEqual(self.probe(t), 0)
        self.assertIn("could not reach them", self.result("task-c1"))


class TestDecision(Base):
    def park(self, sentence=PIN, name="task-c1"):
        out = wpc.request_approval(self.ws, self.task(name, sentence))
        return out["id"]

    def test_approve_applies_exactly_the_parked_pin_and_tells_the_collaborator(self):
        rid = self.park()
        out = wpc.decide(self.ws, rid, True)
        self.assertEqual(out["status"], "approved")
        self.assertEqual(self.bindings(), {ROOM: W})
        [(rec, text)] = self.posted
        self.assertEqual((rec["room"], rec["thread_root"]), (ROOM, "$ask"))
        self.assertIn("approved", text)

    def test_a_request_decides_once(self):
        rid = self.park()
        wpc.decide(self.ws, rid, False)
        again = wpc.decide(self.ws, rid, True)
        self.assertEqual(again["error"], "already decided")
        self.assertEqual(self.bindings(), {})

    def test_decline_applies_nothing_and_tells_the_collaborator(self):
        rid = self.park()
        out = wpc.decide(self.ws, rid, False)
        self.assertEqual((out["status"], self.bindings()), ("declined", {}))
        self.assertIn("declined", self.posted[0][1])

    def test_an_approved_add_hands_the_core_the_owners_add(self):
        rid = self.park(ADD, "task-a1")
        out = wpc.decide(self.ws, rid, True)
        self.assertEqual(out["status"], "approved")
        self.assertIn("install-core-pool.sh", out["next"])

    def test_an_unknown_id_is_an_error(self):
        self.assertEqual(wpc.decide(self.ws, "nope", True)["error"], "no such request")

    def test_the_cli_round_trip(self):
        import contextlib
        import io
        t = self.task("task-c1", PIN)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(wpc.main(["request", "--task-file", str(t),
                                       "--workspace", str(self.ws)]), 0)
        rid = json.loads(buf.getvalue())["id"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wpc.main(["approve", rid, "--workspace", str(self.ws)]), 0)
            self.assertEqual(wpc.main(["approve", rid, "--workspace", str(self.ws)]), 1)
        self.assertEqual(self.bindings(), {ROOM: W})


if __name__ == "__main__":
    unittest.main(verbosity=2)
