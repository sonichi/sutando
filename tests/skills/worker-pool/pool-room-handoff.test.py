#!/usr/bin/env python3
"""Handing a bound room from the core's agent to a worker's own agent (`pool_room_handoff.py`) and its
`pool_identity.py rooms|hand-room` verbs.

- The core invites the worker's agent, waits until the room lists it joined, leaves, and only then the
  room's bindings (every server name it carries) are dropped.
- The core never leaves a room the worker's agent is not confirmed in, right before leaving: a refused
  or held invitation, an agent that has not joined, or a members read that fails keeps the core and the
  binding.
- A second run resumes: no second invitation to a member, and a core that already left is checked
  through the worker's own agent before the binding is dropped: only a read that shows the worker's agent
  joined and the core gone counts; a failed core read with the core still listed is `unknown`, retryable.

The Actions are an in-memory fake; nothing here reaches a network.

Run: python3 tests/skills/worker-pool/pool-room-handoff.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "skills" / "worker-pool" / "scripts"), str(ROOT / "src")]

import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock

import connections
import pool_identity
import pool_provision as pp
import pool_room_handoff as rh
import pool_roster as pr

W, OTHER = "7c700e11" + "0" * 24, "8d811f22" + "0" * 24
CORE, AGENT, OWNER = "@desk.agent:s.space", "@desk-reviewer.agent:s.space", "@owner:s.space"
ROOM, LEGACY, LISTED = "!aaa:s.space", "!aaa:ag2.space", "!bbb:s.space"


class FakeActions:
    """room.members.list, room.member.invite and room.leave as one agent; `joins` admits the invitee
    after that many members reads."""

    def __init__(self, members, *, me=CORE, invite=None, leave=None, joins=0, read_error=None):
        self.members, self.me, self.joins, self.read_error = set(members), me, joins, read_error
        self.invite = invite if invite is not None else {"result": {"invited": True, "expected_admission": "admitted"}}
        self.leave = leave if leave is not None else {"result": {}}
        self.calls, self.invited, self.ops, self.invites = [], "", {}, []

    def read(self, action, arguments, room_id):
        self.calls.append(("read", action, room_id))
        if self.read_error is not None:
            if isinstance(self.read_error, Exception):
                raise self.read_error
            return self.read_error
        if self.invited and self.joins <= 0:
            self.members.add(self.invited)
        self.joins -= 1
        return {"result": {"members": [{"user_id": m, "display_name": ""} for m in sorted(self.members)]}}

    def read_account(self, action, arguments):
        self.calls.append(("read_account", action))
        if isinstance(self.invites, Exception):
            raise self.invites
        return self.invites if isinstance(self.invites, dict) else \
            {"result": {"invites": [{"room_id": r} for r in self.invites]}}

    def execute(self, action, arguments, *, operation_id="", room_id=""):
        self.calls.append((action, operation_id, room_id))
        if operation_id in self.ops:
            return self.ops[operation_id]  # the server deduplicates an operation id: no second effect
        self.ops[operation_id] = self._act(action, arguments)
        return self.ops[operation_id]

    def _act(self, action, arguments):
        if action == "room.member.invite":
            if pp.approval_state(self.invite) == "done" and pp.find(self.invite, "invited") is not False:
                self.invited = arguments["user_id"]
            return self.invite
        if pp.approval_state(self.leave) == "done":
            self.members.discard(self.me)
        return self.leave


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        (self.ws / "state").mkdir()
        workers = {W: {"label": "reviewer", "state": "live"}, OTHER: {"label": "other", "state": "live"}}
        bindings = {ROOM: W, LEGACY: W, LISTED: [W], "!ccc:s.space": OTHER}
        pr.save_bindings(self.ws, bindings)
        pr.compile_roster(self.ws, workers, bindings)
        self.row = {"worker": W, "agent_mxid": AGENT, "instance": "s-space-7c700e11"}
        ident = mock.patch.object(connections, "worker_identity", lambda w, ws=None: dict(self.row) if w == W else None)
        ident.start()
        self.addCleanup(ident.stop)
        self.clock, self.wall = [0.0], [1_000_000.0]

    def hand(self, core, room=ROOM, *, worker=None, **kw):
        kw.setdefault("as_worker", lambda: worker or FakeActions(set(), me=AGENT, read_error={"is_error": True}))
        kw.setdefault("core_agent", CORE)
        kw.setdefault("wall", lambda: self.wall[0])
        return rh.hand_room(W, room, workspace=self.ws, actions=core, server="s.space", sleep=self.sleep,
                            clock=lambda: self.clock[0], **kw)

    def sleep(self, s):
        self.clock[0] += s

    def bound(self):
        return pr.load_bindings(self.ws)


class HandRoom(Base):
    def test_invite_join_leave_then_unbind_every_server_name(self):
        core = FakeActions({CORE, OWNER})
        out = self.hand(core)
        self.assertEqual((out["status"], out["core_left"], out["room"]), ("handed", True, ROOM))
        self.assertEqual(out["bindings"], [LEGACY, ROOM])
        self.assertEqual([c[0] for c in core.calls if c[0] != "read"], ["room.member.invite", "room.leave"])
        invite = next(c for c in core.calls if c[0] == "room.member.invite")
        self.assertEqual(invite[1], rh.operation_id("invite", W, AGENT, ROOM))
        self.assertNotIn(ROOM, self.bound())
        self.assertNotIn(LEGACY, self.bound())
        self.assertEqual(self.bound()["!ccc:s.space"], OTHER, "another worker's room is untouched")
        self.assertEqual(self.hand(FakeActions({CORE}))["status"], "not_bound", "a second run finds nothing to do")

    def test_a_member_already_joined_is_not_invited_again(self):
        core = FakeActions({CORE, OWNER, AGENT})
        self.assertEqual(self.hand(core, LEGACY)["status"], "handed")
        self.assertNotIn("room.member.invite", [c[0] for c in core.calls])
        self.assertTrue(all(c[-1] == ROOM for c in core.calls), "acts on the room's id on the core's server")

    def test_waits_for_the_join_within_the_bound_then_says_waiting(self):
        core = FakeActions({CORE, OWNER}, joins=3)
        out = self.hand(core, wait_s=60.0, poll_s=5.0)
        self.assertEqual(out["status"], "handed")
        self.assertEqual(self.clock[0], 10.0)
        core = FakeActions({CORE, OWNER}, joins=99, invite={"result": {"invited": True, "expected_admission": "held",
                                                                     "waiting_on": "owner_approval"}})
        self.setUp()
        out = self.hand(core, wait_s=12.0, poll_s=5.0)
        self.assertEqual((out["status"], out["waiting_on"], out["expected_admission"]),
                         ("waiting", "owner_approval", "held"))
        self.assertNotIn("room.leave", [c[0] for c in core.calls], "the core never leaves before the join")
        self.assertIn(ROOM, self.bound())

    def test_a_refused_or_held_invitation_keeps_the_core_and_the_binding(self):
        for invite, status, says in (
                ({"is_error": True, "message": "not allowed to invite"}, "skipped", "not allowed to invite"),
                ({"result": {"invited": False, "reason": "the agent declines invites to direct chats"}}, "skipped",
                 "declines invites to direct chats"),
                ({"status": "approval_required", "approval_id": "ap1"}, "waiting", "owner's approval")):
            self.setUp()
            core = FakeActions({CORE, OWNER}, invite=invite)
            out = self.hand(core)
            self.assertEqual(out["status"], status, invite)
            self.assertIn(says, out["why"])
            self.assertNotIn("room.leave", [c[0] for c in core.calls])
            self.assertIn(ROOM, self.bound())

    def test_a_refused_or_held_leave_keeps_the_binding(self):
        for leave, status in (({"is_error": True, "message": "nope"}, "failed"),
                              ({"status": "approval_required", "approval_id": "ap2"}, "waiting")):
            self.setUp()
            out = self.hand(FakeActions({CORE, AGENT}, leave=leave))
            self.assertEqual(out["status"], status)
            self.assertIn(ROOM, self.bound())

    def test_the_join_is_confirmed_again_right_before_leaving(self):
        core = FakeActions({CORE, AGENT})
        reads = iter([{"result": {"members": [{"user_id": CORE}, {"user_id": AGENT}]}},
                      {"is_error": True, "message": "rate limited"}])
        core.read = lambda *a: next(reads)
        out = self.hand(core)
        self.assertEqual(out["status"], "failed")
        self.assertIn("rate limited", out["why"])
        self.assertIn(ROOM, self.bound())

    def test_a_core_that_already_left_is_checked_through_the_worker_agent(self):
        gone = {"is_error": True, "message": "not a member"}
        out = self.hand(FakeActions(set(), read_error=gone), worker=FakeActions({AGENT, OWNER}, me=AGENT))
        self.assertEqual((out["status"], out["core_left"]), ("handed", False))
        self.assertNotIn(ROOM, self.bound())
        self.setUp()
        out = self.hand(FakeActions(set(), read_error=gone), worker=FakeActions({OWNER}, me=AGENT))
        self.assertEqual(out["status"], "failed")
        self.assertIn("not a member", out["why"])
        self.assertIn(ROOM, self.bound())

    def invite_ids(self, core):
        return [int(c[1].rsplit("-", 1)[1]) for c in core.calls if c[0] == "room.member.invite"]

    def test_a_sent_invitation_moves_on_only_once_the_agent_no_longer_holds_it(self):
        core = FakeActions({CORE, OWNER}, joins=10 ** 6)
        worker_side = FakeActions(set(), me=AGENT)
        worker_side.invites = [ROOM]
        for _ in range(2):
            self.assertEqual(self.hand(core, worker=worker_side)["status"], "waiting")
        for unreadable in ({"is_error": True, "message": "rate limited"}, pp.ProvisionError("http 502")):
            worker_side.invites = unreadable
            self.assertEqual(self.hand(core, worker=worker_side)["status"], "waiting")
        self.wall[0] += 10 ** 6
        self.assertEqual(self.invite_ids(core), [0, 0, 0, 0], "held, or not known to be gone: the same operation")
        core.invited, core.joins, worker_side.invites = "", 0, []  # declined: the agent holds no invitation
        self.assertEqual(self.hand(core, worker=worker_side)["status"], "handed")
        self.assertEqual(self.invite_ids(core)[-1], 1)

    def test_an_error_or_held_answer_keeps_the_operation_id(self):
        for answer in ({"is_error": True, "message": "upstream unavailable"},
                       {"is_error": True, "details": {"reason": "mint_failed"}},
                       {"is_error": True, "status": "approval_required"},
                       {"status": "approval_required", "approval_id": "ap"}):
            self.setUp()
            core = FakeActions({CORE}, invite=answer)
            self.hand(core)
            self.hand(core)
            self.assertEqual(self.invite_ids(core), [0, 0], answer)
            self.setUp()
            core = FakeActions({CORE, AGENT}, leave=answer)
            self.hand(core)
            core.ops.clear()
            self.hand(core)
            self.assertEqual({c[1] for c in core.calls if c[0] == "room.leave"},
                             {rh.operation_id("leave", W, AGENT, ROOM, 0)}, answer)
            self.assertIn(ROOM, self.bound())

    def test_a_definite_no_moves_to_a_new_operation_id(self):
        for answer in ({"result": {"invited": False, "reason": "declines"}}, {"status": "denied"}):
            self.setUp()
            core = FakeActions({CORE}, invite=answer)
            self.assertEqual(self.hand(core)["status"], "skipped")
            core.invite, core.joins = {"result": {"invited": True}}, 0
            self.assertEqual(self.hand(core)["status"], "handed", answer)
            self.assertEqual(self.invite_ids(core), [0, 1])

    def test_an_account_read_names_no_room(self):
        acts = pp.Actions("https://gw", "tok", None, [])
        with mock.patch.object(acts, "tool", return_value={"result": {}}) as tool:
            acts.read_account("room.invites.list", {})
        tool.assert_called_once_with("room.action.read", {"action": "room.invites.list", "target_kind": "account",
                                                          "arguments": {}})

    def test_records_for_other_rooms_made_at_once_all_survive(self):
        import threading
        import time as _time
        real = rh._attempts

        def slow(ws, worker):
            data = real(ws, worker)
            _time.sleep(0.01)
            return data
        with mock.patch.object(rh, "_attempts", slow):
            threads = [threading.Thread(target=rh.record_attempt, args=(self.ws, W, f"!r{i}:s.space", "invite", 0,
                                                                         "sent", 1.0)) for i in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(sorted(rh._attempts(self.ws, W)), sorted(f"!r{i}:s.space" for i in range(12)))

    def test_a_later_hand_off_never_reuses_an_earlier_leave(self):
        core = FakeActions({CORE, AGENT})
        self.assertEqual(self.hand(core)["status"], "handed")
        pr.bind_room(self.ws, ROOM, W)
        core.members = {CORE, AGENT}  # the core was added to the room again
        out = self.hand(core)
        self.assertEqual((out["status"], out["core_left"]), ("handed", True))
        leaves = [c[1] for c in core.calls if c[0] == "room.leave"]
        self.assertEqual(leaves, [rh.operation_id("leave", W, AGENT, ROOM, n) for n in (0, 1)])
        self.assertNotIn(CORE, core.members, "the second leave acted")

    def test_a_held_leave_keeps_its_operation_id(self):
        core = FakeActions({CORE, AGENT}, leave={"status": "approval_required", "approval_id": "ap"})
        self.assertEqual(self.hand(core)["status"], "waiting")
        core.ops.clear()
        core.leave = {"result": {}}
        self.wall[0] += 10 ** 6
        self.assertEqual(self.hand(core)["status"], "handed")
        self.assertEqual({c[1] for c in core.calls if c[0] == "room.leave"}, {rh.operation_id("leave", W, AGENT, ROOM, 0)})

    def test_an_unreadable_attempts_record_starts_again_at_zero(self):
        path = rh._attempts_path(self.ws, W)
        path.parent.mkdir(parents=True, exist_ok=True)
        for text in ("{torn", "[]"):
            path.write_text(text)
            self.assertEqual(rh.attempt(self.ws, W, ROOM, "invite"), 0)

    def test_a_member_entry_that_is_not_join_is_not_joined(self):
        for membership in ("invite", "leave"):
            self.setUp()
            core = FakeActions({CORE, OWNER}, joins=99)
            listed = {"result": {"members": [{"user_id": CORE, "display_name": ""},
                                             {"user_id": AGENT, "display_name": "", "membership": membership}]}}
            core.read = lambda *a: core.calls.append(("read",)) or listed
            out = self.hand(core)
            self.assertEqual(out["status"], "waiting", membership)
            self.assertNotIn("room.leave", [c[0] for c in core.calls], "the core stays")
            self.assertEqual((self.bound()[ROOM], self.bound()[LEGACY]), (W, W), "binding kept")

    def test_a_failed_core_read_is_never_taken_as_the_core_having_left(self):
        limited = {"is_error": True, "message": "rate limited"}
        for worker_side, core_agent in ((FakeActions({AGENT, CORE, OWNER}, me=AGENT), CORE),
                                        (FakeActions({AGENT, OWNER}, me=AGENT), ""),
                                        (FakeActions(set(), me=AGENT, read_error=limited), CORE)):
            self.setUp()
            core = FakeActions({CORE, AGENT}, read_error=limited)
            out = self.hand(core, worker=worker_side, core_agent=core_agent)
            self.assertEqual(out["status"], "unknown", worker_side.members)
            self.assertIn("rate limited", out["why"])
            self.assertIn("Run this again", out["why"])
            self.assertEqual([c for c in core.calls if c[0] != "read"], [], "no leave was assumed or sent")
            self.assertEqual((self.bound()[ROOM], self.bound()[LEGACY]), (W, W), "both bindings kept")

    def test_a_listed_binding_a_worker_without_an_agent_and_a_transport_failure(self):
        self.assertEqual(self.hand(FakeActions({CORE, AGENT}), LISTED)["status"], "handed")
        self.assertNotIn(LISTED, self.bound())
        out = self.hand(FakeActions({CORE}, read_error=pp.ProvisionError("http 502")))
        self.assertEqual(out["status"], "failed")
        self.assertIn("http 502", out["why"])
        self.row["disabled"] = True
        self.assertIn("no enabled agent", self.hand(FakeActions({CORE}))["why"])
        self.assertEqual(self.bound()[ROOM], W)

    def test_the_core_and_worker_actions_come_from_their_own_rows(self):
        core = FakeActions({CORE, AGENT})
        with mock.patch.object(pp, "core_actions", return_value=(core, {"server": "s.space"})) as made:
            out = rh.hand_room(W, ROOM, workspace=self.ws)
        self.assertEqual(out["status"], "handed")
        made.assert_called_once_with(self.ws, vault=None)
        gone = FakeActions(set(), read_error={"is_error": True, "message": "not a member"})
        self.setUp()
        with mock.patch.object(pp, "core_actions", return_value=(gone, {"server": "s.space", "agent_mxid": CORE})):
            out = rh.hand_room(W, ROOM, workspace=self.ws, as_worker=lambda: FakeActions({AGENT}, me=AGENT))
        self.assertEqual(out["status"], "handed", "the core's own id comes from its row")
        with mock.patch.object(connections, "connection_credential", return_value=("https://gw", "https://gw|tok")):
            acts = rh.worker_actions(self.row)
        self.assertEqual((acts.gateway, acts.bearer), ("https://gw", "tok"))


class WorkedRooms(Base):
    def task(self, tid, room, *, archived=False, worker=W, header=True):
        import pool_attribution as pa
        d = self.ws / "tasks" / ("archive" if archived else "")
        d.mkdir(parents=True, exist_ok=True)
        lines = ["id: " + tid, "source: ag2space"] + ([f"channel_id: {room}"] if header and room else [])
        (d / f"{tid}.txt").write_text("\n".join(lines + ["task: hello"]) + "\n")
        self.assertTrue(pa.record(self.ws, tid, worker))

    def test_the_rooms_its_tasks_and_their_interactions_came_from_minus_bound_ones(self):
        import correlation_log
        self.task("task-1", "!rev:ag2.space")
        self.task("task-2", "!rev:s.space", archived=True)
        self.task("task-3", "!ops:s.space", archived=True)
        self.task("task-4", ROOM)
        self.task("task-5", "123456789")
        self.task("task-6", "!theirs:s.space", worker=OTHER)
        self.task("task-7", "", header=False)
        (self.ws / "state" / "attribution" / "task-8").write_text(W)
        correlation_log.record("interaction", {"interaction_id": correlation_log.mint_interaction_id(), "object": "o/r#1", "counterpart": "@x:s.space",
                                               "counterpart_kind": "agent", "counterpart_scope": "unknown",
                                               "room_id": "!triage:s.space", "room_kind": "unknown", "task_id": "task-7",
                                               "channel": "room", "event_id": None}, workspace=self.ws)
        correlation_log.record("interaction", {"interaction_id": correlation_log.mint_interaction_id(), "object": "o/r#2", "counterpart": "@x:s.space",
                                               "counterpart_kind": "agent", "counterpart_scope": "unknown",
                                               "room_id": "!elsewhere:s.space", "room_kind": "unknown",
                                               "task_id": "task-6", "channel": "room", "event_id": None},
                               workspace=self.ws)
        self.assertEqual(rh.worked_rooms(self.ws, W, "s.space"), [
            {"room": "!ops:s.space", "tasks": 1}, {"room": "!rev:s.space", "tasks": 2},
            {"room": "!triage:s.space", "tasks": 1}])

    def test_no_attribution_record_means_no_worked_rooms(self):
        self.assertEqual(rh.worked_rooms(self.ws, W, "s.space"), [])

    def test_join_room_invites_and_waits_and_the_core_stays(self):
        core = FakeActions({CORE, OWNER}, joins=1)
        out = rh.join_room(W, "!rev:ag2.space", workspace=self.ws, actions=core, server="s.space", wait_s=10,
                           poll_s=5, sleep=self.sleep, clock=lambda: self.clock[0])
        self.assertEqual((out["status"], out["room"]), ("joined", "!rev:s.space"))
        self.assertNotIn("room.leave", [c[0] for c in core.calls])
        self.assertEqual(self.bound()[ROOM], W, "no binding changes")
        out = rh.join_room(W, "!rev:s.space", workspace=self.ws, actions=FakeActions({CORE, AGENT}), server="s.space")
        self.assertEqual(out["status"], "joined")
        for core, status in ((FakeActions({CORE}, invite={"is_error": True, "message": "direct chat"}), "skipped"),
                             (FakeActions({CORE}, joins=99), "waiting"),
                             (FakeActions(set(), read_error={"is_error": True, "message": "not a member"}), "skipped"),
                             (FakeActions(set(), read_error=pp.ProvisionError("http 502")), "failed")):
            out = rh.join_room(W, "!rev:s.space", workspace=self.ws, actions=core, server="s.space",
                               sleep=self.sleep, clock=lambda: self.clock[0],
                               as_worker=lambda: FakeActions(set(), me=AGENT))
            self.assertEqual(out["status"], status, out)
        self.row["disabled"] = True
        self.assertEqual(rh.join_room(W, "!rev:s.space", workspace=self.ws, actions=FakeActions({CORE}))["status"],
                         "failed")


class Cli(Base):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = pool_identity.main(["--workspace", str(self.ws), *argv])
        return rc, out.getvalue()

    def test_rooms_lists_each_bound_room_once(self):
        with mock.patch.object(pp, "core_lane", return_value={"server": "s.space"}):
            rc, out = self.run_cli("rooms", W)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"worker": W, "rooms": [
            {"room": ROOM, "bindings": [LEGACY, ROOM]}, {"room": LISTED, "bindings": [LISTED]}]})

    def test_worked_lists_the_unbound_rooms_it_worked_in(self):
        with mock.patch.object(pp, "core_lane", return_value={"server": "s.space"}), \
                mock.patch.object(rh, "worked_rooms", return_value=[{"room": "!rev:s.space", "tasks": 2}]) as worked:
            rc, out = self.run_cli("worked", W)
        self.assertEqual((rc, json.loads(out)), (0, {"worker": W, "rooms": [{"room": "!rev:s.space", "tasks": 2}]}))
        worked.assert_called_once_with(self.ws, W, "s.space")

    def test_join_room_prints_one_line_and_fails_only_on_failed(self):
        for status, rc in (("joined", 0), ("skipped", 0), ("failed", 1)):
            with mock.patch.object(rh, "join_room", return_value={"status": status}) as join:
                self.assertEqual(self.run_cli("join-room", W, "--room", "!rev:s.space"),
                                 (rc, json.dumps({"status": status}) + "\n"))
            join.assert_called_once_with(W, "!rev:s.space", workspace=self.ws, wait_s=0.0)

    def test_hand_room_prints_one_line_and_fails_only_on_failed(self):
        for status, rc in (("handed", 0), ("waiting", 0), ("failed", 1)):
            with mock.patch.object(rh, "hand_room", return_value={"status": status}) as hand:
                self.assertEqual(self.run_cli("hand-room", "reviewer", "--room", ROOM, "--wait", "30"),
                                 (rc, json.dumps({"status": status}) + "\n"))
            hand.assert_called_once_with(W, ROOM, workspace=self.ws, wait_s=30.0)


if __name__ == "__main__":
    unittest.main()
