#!/usr/bin/env python3
"""skills/owner-agent-consult: inert until configured; the owner gate reads only a live,
envelope-verified, unanswered task by id; the owner-only room guard accounts for every
member; an ask is a plain message mentioning the agent, in one consult thread, and returns
at once with a pending record; an answer is any message from the asked agent in that thread
after the ask, correlated by Matrix relations and this agent's own records only; onward asks
stay in the thread; no other skill imported; no identity or room literal shipped. Workspaces are temp dirs; the production-adapter
cases run the real room_ops CLI against a local fake gateway."""
import contextlib
import http.server
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[3]
SKILL = REPO / "skills" / "owner-agent-consult"
ROOM_OPS_CLI = REPO / "skills" / "agent-room-ops" / "room_ops.py"
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(REPO / "src"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


policy = _load("consult_policy", SKILL / "scripts" / "consult_policy.py")
cli = _load("consult_cli", SKILL / "scripts" / "consult.py")
import task_envelope as te  # noqa: E402

ROOM = "!consult:example.test"
SELF = "@self.agent:example.test"
OWNER = "@owner:example.test"
SIB = "@sibling.agent:example.test"
THIRD = "@third.agent:example.test"
FOURTH = "@fourth.agent:example.test"
OTHER_AGENT = "@foreign.agent:example.test"
STRANGER = "@stranger:example.test"
CONFIG_KEYS = (policy.CONFIG_ROOM, policy.CONFIG_ROOM_CLI, policy.CONFIG_ENABLED,
               policy.CONFIG_NUDGE_AFTER)
CID = "0123456789abcdef"
CID_B = "fedcba9876543210"
TID = "task-1"


OWNER_DM = "!owner-dm:example.test"


def task_text(tier="owner", body="what is on the build host?", extra="", tid=TID):
    head = (f"id: {tid}\nsource: ag2space\nchannel_id: {OWNER_DM}\nsource_message_id: $owner-msg\n"
            + (f"access_tier: {tier}\n" if tier is not None else ""))
    return head + extra + f"task: {body}\n"


def reply_task(ws, event, *, tid="task-reply", room=ROOM, sender=SIB, stamp=True, body="answer"):
    text = (f"id: {tid}\nsource: ag2space\nchannel_id: {room}\nsource_message_id: {event}\n"
            f"user_id: {sender}\naccess_tier: team\ntask: {SELF} — {body}\n")
    if stamp:
        text = te.stamp_text(text, ws)
    (Path(ws) / "tasks" / f"{tid}.txt").write_text(text, encoding="utf-8")
    return tid


def make_ws(root, text=None, *, stamp=True, tid=TID):
    ws = Path(root)
    (ws / "tasks").mkdir(parents=True, exist_ok=True)
    (ws / "results").mkdir(parents=True, exist_ok=True)
    text = task_text(tid=tid) if text is None else text
    if stamp:
        text = te.stamp_text(text, ws)
    (ws / "tasks" / f"{tid}.txt").write_text(text, encoding="utf-8")
    return ws


def final(cid, text):
    """An answer is a plain message; the consult id never appears in it."""
    return text


class Room:
    """One consult room's timeline, shared by the agents' transports."""

    def __init__(self):
        self.msgs = []  # oldest first
        self.clock = time.time  # the server's clock: stamps each event's ts in ms

    def post(self, sender, body, reply_to=None, thread_root=None):
        eid = "$ask" if not self.msgs else f"$ask{len(self.msgs) + 1}"
        self.msgs.append({"sender": sender, "body": body, "event_id": eid, "ts": int(self.clock() * 1000),
                          **({"in_reply_to": reply_to} if reply_to else {}),
                          **({"thread_root": thread_root} if thread_root else {})})
        return eid


class FakeTransport:
    def __init__(self, members=None, agents=None, replies=None, unidentified=0, me=SELF, room=None):
        self._members = members if members is not None else [
            {"user_id": SELF, "kind": "agent"}, {"user_id": OWNER, "kind": "human"},
            {"user_id": SIB, "kind": "agent", "display_name": "Sibling"}]
        self._agents = agents if agents is not None else [
            {"id": SELF, "owner": OWNER}, {"id": SIB, "owner": OWNER}, {"id": THIRD, "owner": OWNER},
            {"id": OTHER_AGENT, "owner": STRANGER}]
        self.unidentified = unidentified
        self.replies = replies
        self.me, self.room = me, room if room is not None else Room()
        self.posted = []
        self.reply_tos = []
        self.thread_roots = []
        self.reads = 0

    def members(self, room):
        return {"ok": True, "members": list(self._members), "unidentified": self.unidentified}

    def agents(self):
        return {"ok": True, "agents": list(self._agents)}

    def mention(self, mxid, body, room, reply_to=None, thread_root=None):
        self.posted.append((mxid, body, room))
        self.reply_tos.append(reply_to)
        self.thread_roots.append(thread_root)
        return {"ok": True, "event_id": self.room.post(self.me, f"{mxid} — {body}", reply_to, thread_root)}

    def read(self, room, limit):
        self.reads += 1
        if self.replies is None:
            return {"ok": True, "messages": list(reversed(self.room.msgs))[:limit]}
        msgs = self.replies(self.reads) if callable(self.replies) else self.replies
        return {"ok": True, "messages": msgs}


def run_consult(tr, ws=None, *, text=None, stamp=True, question="is the build host up?",
                agent=SIB, cid=CID, tid=TID, now=None):
    with contextlib.ExitStack() as stack:
        if ws is None:
            ws = make_ws(stack.enter_context(tempfile.TemporaryDirectory()), text, stamp=stamp, tid=tid)
        return policy.consult(tr, room=ROOM, self_mxid=SELF, agent=agent, question=question,
                              task_id=tid, workspace=ws, cid=cid,
                              now=(lambda: now) if now is not None else None)


def match(tr, ws, tid="task-reply"):
    return policy.match_reply(tr, room=ROOM, self_mxid=SELF, task_id=tid, workspace=ws)


def pending_ids(ws):
    return [r["cid"] for r in policy.records(ws, "pending")]


class TestInert(unittest.TestCase):
    def _cli(self, *argv, env=None):
        out = io.StringIO()
        clean = {k: v for k, v in os.environ.items() if k not in CONFIG_KEYS}
        tr = FakeTransport()
        with mock.patch.dict(os.environ, {**clean, **(env or {})}, clear=True), \
                contextlib.redirect_stdout(out):
            rc = cli.main(list(argv), transport=tr, workspace=Path(tempfile.gettempdir()))
        return rc, json.loads(out.getvalue()), tr

    def test_unconfigured_is_inert_and_says_so(self):
        rc, res, tr = self._cli("roster", "--agent", SELF)
        self.assertEqual(rc, 0)
        self.assertTrue(res["inert"])
        self.assertIn(policy.CONFIG_ROOM, res["reason"])
        self.assertEqual(tr.posted, [])

    def test_no_room_transport_is_inert(self):
        rc, res, _ = self._cli("roster", "--agent", SELF, env={policy.CONFIG_ROOM: ROOM})
        self.assertTrue(res["inert"])
        self.assertIn(policy.CONFIG_ROOM_CLI, res["reason"])

    def test_shipped_manifest_names_no_room_or_transport(self):
        cfg = policy.manifest_config()
        self.assertEqual(cfg.get(policy.CONFIG_ROOM), "")
        self.assertEqual(cfg.get(policy.CONFIG_ROOM_CLI), "")
        self.assertFalse(policy.settings(environ={})["active"])

    def test_disabled_flag_is_inert_even_with_a_room(self):
        rc, res, _ = self._cli("roster", "--agent", SELF,
                               env={policy.CONFIG_ROOM: ROOM, policy.CONFIG_ROOM_CLI: "/x",
                                    policy.CONFIG_ENABLED: "0"})
        self.assertTrue(res["inert"])

    def test_configured_roster_runs(self):
        rc, res, _ = self._cli("roster", "--agent", SELF,
                               env={policy.CONFIG_ROOM: ROOM, policy.CONFIG_ROOM_CLI: "/x"})
        self.assertTrue(res["ok"], res)
        self.assertEqual([a["mxid"] for a in res["agents"]], [SIB])

    def test_nudge_time_is_clamped(self):
        for raw, want in (("99999999", policy.NUDGE_CEILING_S), ("1", policy.NUDGE_FLOOR_S)):
            s = policy.settings(environ={policy.CONFIG_ROOM: ROOM, policy.CONFIG_NUDGE_AFTER: raw},
                                manifest_cfg={})
            self.assertEqual(s["nudge_after_s"], want)


class TestOwnerOnlyGuard(unittest.TestCase):
    def test_admits_owner_self_and_owner_agents(self):
        self.assertTrue(policy.guard(ROOM, SELF, FakeTransport())["ok"])

    def test_refuses_a_non_owner_human_and_names_it(self):
        tr = FakeTransport()
        tr._members.append({"user_id": STRANGER, "kind": "human"})
        v = policy.guard(ROOM, SELF, tr)
        self.assertFalse(v["ok"])
        self.assertIn(STRANGER, v["reason"])
        self.assertFalse(run_consult(tr)["asked"])
        self.assertEqual(tr.posted, [])

    def test_refuses_another_owners_agent(self):
        tr = FakeTransport()
        tr._members.append({"user_id": OTHER_AGENT, "kind": "agent"})
        v = policy.guard(ROOM, SELF, tr)
        self.assertFalse(v["ok"])
        self.assertIn(OTHER_AGENT, v["reason"])

    def test_an_agent_looking_mxid_outside_the_registry_is_refused(self):
        tr = FakeTransport()
        tr._members.append({"user_id": "@sutando-lookalike:example.test", "kind": "agent"})
        self.assertFalse(policy.guard(ROOM, SELF, tr)["ok"])

    def test_refuses_when_registry_names_no_owner(self):
        tr = FakeTransport(agents=[{"id": SIB, "owner": OWNER}])
        v = policy.guard(ROOM, SELF, tr)
        self.assertFalse(v["ok"])
        self.assertIn("no owner", v["reason"])

    def test_refuses_unknown_self_and_unreadable_members(self):
        self.assertFalse(policy.guard(ROOM, "", FakeTransport())["ok"])
        tr = FakeTransport()
        tr.members = lambda room: {"ok": False, "reason": "403"}
        self.assertFalse(policy.guard(ROOM, SELF, tr)["ok"])


class TestEveryMemberAccountedFor(unittest.TestCase):
    """A member the transport could not identify means the room is not proven owner-only."""

    def test_unidentified_member_refuses(self):
        tr = FakeTransport(unidentified=1)
        v = policy.guard(ROOM, SELF, tr)
        self.assertFalse(v["ok"])
        self.assertIn("no identity", v["reason"])
        self.assertFalse(run_consult(tr)["asked"])
        self.assertEqual(tr.posted, [])

    def test_transport_that_does_not_report_the_count_refuses(self):
        tr = FakeTransport()
        tr.members = lambda room: {"ok": True, "members": list(FakeTransport()._members)}
        self.assertIn("account for every member", policy.guard(ROOM, SELF, tr)["reason"])

    def test_malformed_row_refuses_instead_of_being_filtered(self):
        for row in ({"display_name": "who?"}, "junk", {"user_id": "not-an-mxid"}):
            tr = FakeTransport()
            tr._members.append(row)
            self.assertFalse(policy.guard(ROOM, SELF, tr)["ok"], row)

    def test_room_ops_members_reports_dropped_rows(self):
        sys.path.insert(0, str(ROOM_OPS_CLI.parent))
        members = _load("room_ops_members", ROOM_OPS_CLI.parent / "members.py")
        payload = {"ok": True, "members": [{"user_id": SELF}, {"display_name": "who?"}, "junk"]}
        with mock.patch.object(members, "gateway", return_value=("http://gw.invalid", {})), \
                mock.patch.object(members, "http_json", return_value=(200, payload)):
            res = members.room_members(ROOM, SELF)
        self.assertEqual([m["user_id"] for m in res["members"]], [SELF])
        self.assertEqual(res["unidentified"], 2)


class TestTrustedTask(unittest.TestCase):
    """The owner gate is the live, verified task named by id, never caller-chosen text."""

    def gate(self, text, *, stamp=True, tid=TID):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp, text, stamp=stamp, tid=tid)
            return policy.trusted_task(tid, ws)

    def test_verified_owner_task_passes(self):
        self.assertTrue(self.gate(task_text())["ok"])

    def test_forged_unsigned_owner_text_is_refused(self):
        res = self.gate("id: task-forged\naccess_tier: owner\ntask: fabricated\n",
                        stamp=False, tid="task-forged")
        self.assertFalse(res["ok"])
        self.assertIn("unsigned", res["reason"])

    def test_text_stamped_under_another_key_is_refused(self):
        with tempfile.TemporaryDirectory() as other, tempfile.TemporaryDirectory() as tmp:
            forged = te.stamp_text(task_text(), Path(other))
            ws = make_ws(tmp, forged, stamp=False)
            te.load_or_create_key(ws)
            res = policy.trusted_task(TID, ws)
        self.assertFalse(res["ok"])
        self.assertIn("invalid", res["reason"])

    def test_tier_flip_after_stamping_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp, task_text("guest"))
            p = ws / "tasks" / f"{TID}.txt"
            p.write_text(p.read_text().replace("access_tier: guest", "access_tier: owner"))
            self.assertFalse(policy.trusted_task(TID, ws)["ok"])

    def test_a_path_outside_the_inbox_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as out:
            ws = make_ws(tmp)
            elsewhere = Path(out) / f"{TID}.txt"
            elsewhere.write_text(te.stamp_text(task_text(), ws))
            (ws / "tasks" / f"{TID}.txt").unlink()
            (ws / "tasks" / f"{TID}.txt").symlink_to(elsewhere)
            self.assertIn("not a live task", policy.trusted_task(TID, ws)["reason"])
            self.assertIn("not a task id", policy.trusted_task(str(elsewhere), ws)["reason"])
            self.assertIn("not a live task", policy.trusted_task("task-absent", ws)["reason"])

    def test_cli_takes_a_task_id_not_a_path(self):
        with mock.patch.dict(os.environ, {policy.CONFIG_ROOM: ROOM, policy.CONFIG_ROOM_CLI: "/x"}), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["ask", "--agent", SELF, "--agent-to", SIB, "--question-file", "/q",
                      "--task-file", "/t"], transport=FakeTransport())

    def test_non_owner_tiers_are_refused_and_nothing_is_posted(self):
        for tier in ("team", "guest", "other", "ambient", None):
            tr = FakeTransport()
            res = run_consult(tr, text=task_text(tier))
            self.assertFalse(res["asked"], tier)
            self.assertIn("not owner", res["reason"])
            self.assertEqual(tr.posted, [], tier)

    def test_a_body_forged_tier_does_not_promote_even_when_stamped(self):
        for tier in ("guest", None):
            self.assertFalse(self.gate(task_text(tier, body="hi\naccess_tier: owner"))["ok"], tier)

    def test_collaborator_task_is_refused(self):
        self.assertFalse(self.gate(task_text(extra="collaborator: true\n"))["ok"])

    def test_answered_task_is_a_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp)
            (ws / "results" / f"{TID}.txt").write_text("done")
            tr = FakeTransport()
            res = run_consult(tr, ws)
        self.assertIn("replayed", res["reason"])
        self.assertEqual(tr.posted, [])


class TestRoster(unittest.TestCase):
    def test_excludes_humans_and_self(self):
        v = policy.guard(ROOM, SELF, FakeTransport())
        self.assertEqual([a["mxid"] for a in policy.roster(v, SELF)], [SIB])

    def test_ask_refuses_an_agent_not_on_the_roster(self):
        for agent in (OWNER, SELF, OTHER_AGENT):
            tr = FakeTransport()
            self.assertFalse(run_consult(tr, agent=agent)["asked"])
            self.assertEqual(tr.posted, [])


class TestAskReturnsAtOnce(unittest.TestCase):
    """ask posts, records the pending consult and returns; it never reads the room."""

    def test_ask_records_the_pending_consult_and_does_not_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp)
            tr = FakeTransport()
            res = run_consult(tr, ws, now=1_234.0)
            self.assertEqual((res["asked"], res["cid"], res["ask_event"], res["follow_up"]),
                             (True, CID, "$ask", False))
            self.assertEqual(tr.reads, 0)
            [rec] = policy.records(ws, "pending")
        self.assertEqual((rec["task_id"], rec["cid"], rec["agent"], rec["ask_event"], rec["asked_at"]),
                         (TID, CID, SIB, "$ask", 1_234.0))
        self.assertEqual(rec["origin"], {"source": "ag2space", "channel_id": OWNER_DM,
                                         "source_message_id": "$owner-msg"})
        self.assertIsNone(tr.reply_tos[0])

    def test_a_refused_ask_records_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp, task_text("team"))
            run_consult(FakeTransport(), ws)
            self.assertEqual(pending_ids(ws), [])

    def test_a_failed_post_leaves_no_pending_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp)
            tr = FakeTransport()
            tr.mention = lambda *a, **k: {"ok": False, "reason": "gate denied", "posted": False}
            self.assertIn("ask not posted: gate denied", run_consult(tr, ws)["reason"])
            self.assertEqual(pending_ids(ws), [])


class TestFollowUp(unittest.TestCase):
    """A second ask to the same agent within a task is allowed and lands in the same thread."""

    def test_follow_up_to_the_same_agent_is_posted_in_the_consult_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp)
            tr = FakeTransport()
            t = time.time()
            first = run_consult(tr, ws, cid=CID, now=t)
            second = run_consult(tr, ws, cid=CID_B, question="and the disk?", now=t + 1)
            third = run_consult(tr, ws, cid="00000000000000aa", question="and memory?", now=t + 2)
            self.assertTrue(first["asked"] and second["asked"] and third["asked"])
            self.assertEqual((first["follow_up"], second["follow_up"], third["follow_up"]),
                             (False, True, True))
            self.assertEqual(tr.thread_roots, [None, "$ask", "$ask"])
            self.assertEqual({first["root"], second["root"], third["root"]}, {"$ask"})
            self.assertEqual(sorted(pending_ids(ws)), sorted([CID, CID_B, "00000000000000aa"]))
            self.assertEqual(tr.posted[1][1], "and the disk?")

    def test_a_reused_correlation_id_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp)
            tr = FakeTransport()
            run_consult(tr, ws)
            self.assertIn("already used", run_consult(tr, ws)["reason"])
            self.assertEqual(len(tr.posted), 1)


def answer_msg(cid=CID, text="host is up", event="$f", sender=SIB, **rel):
    rel = {"thread_root": "$ask", "in_reply_to": "$ask", **rel}
    return {"sender": sender, "body": f"{SELF} — {final(cid, text)}", "event_id": event,
            "ts": 9e12, **{k: v for k, v in rel.items() if v}}


class World:
    """Three or four of the owner's agents sharing one consult room, each with its own workspace."""

    AGENTS = (SELF, SIB, THIRD, FOURTH)

    def __init__(self, tmp):
        self.room = Room()
        members = [{"user_id": a, "kind": "agent"} for a in self.AGENTS] + [{"user_id": OWNER, "kind": "human"}]
        agents = [{"id": a, "owner": OWNER} for a in self.AGENTS]
        self.ws = {a: Path(tmp) / a.split(".")[0][1:] for a in self.AGENTS}
        make_ws(self.ws[SELF])
        for a in self.AGENTS[1:]:
            (self.ws[a] / "tasks").mkdir(parents=True)
            (self.ws[a] / "results").mkdir()
        self.tr = {a: FakeTransport(members=members, agents=agents, me=a, room=self.room)
                   for a in self.AGENTS}

    def deliver(self, to, event, tid, *, stamp=True, at=None):
        """The receiving gateway writes the task; `at` is its arrival on the receiver's clock
        (default: the server's clock, i.e. no skew)."""
        sender = next(m["sender"] for m in self.room.msgs if m["event_id"] == event)
        out = reply_task(self.ws[to], event, tid=tid, sender=sender, stamp=stamp)
        at = self.room.clock() if at is None else at
        os.utime(self.ws[to] / "tasks" / f"{tid}.txt", (at, at))
        return out

    def ask(self, me, agent, *, cid, task_id=None, via_task=None, question="is the disk full?", **kw):
        return policy.consult(self.tr[me], room=ROOM, self_mxid=me, agent=agent, question=question,
                              workspace=self.ws[me], task_id=task_id, via_task=via_task, cid=cid, **kw)

    def answer(self, me, text, *, task_id=None, up=None, **kw):
        return policy.answer(self.tr[me], room=ROOM, self_mxid=me, text=text, workspace=self.ws[me],
                             task_id=task_id, up=up, **kw)

    def match(self, me, tid):
        return policy.match_reply(self.tr[me], room=ROOM, self_mxid=me, task_id=tid, workspace=self.ws[me])


C1, C2, C3 = "1111111111111111", "2222222222222222", "3333333333333333"
T0 = 1_760_000_000.0  # a realistic epoch; the room stamps events with it in ms


class TestThreadAndAnswerEdges(unittest.TestCase):
    """Every refusal on the answer, trace and match paths is in-band and posts nothing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)
        self.w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.w.deliver(SIB, "$ask", "task-b1")

    def tearDown(self):
        self._tmp.cleanup()

    def test_answer_refusals(self):
        w = self.w
        self.assertIn("exactly one", w.answer(SIB, "x")["reason"])
        self.assertIn("empty answer", w.answer(SIB, "  ", task_id="task-b1")["reason"])
        w.tr[SIB]._members.append({"user_id": STRANGER, "kind": "human"})
        self.assertIn(STRANGER, w.answer(SIB, "x", task_id="task-b1")["reason"])
        w.tr[SIB]._members.pop()
        w.tr[SIB].mention = lambda *a, **k: {"ok": False, "reason": "gate denied", "posted": False}
        self.assertIn("answer not posted: gate denied", w.answer(SIB, "x", task_id="task-b1")["reason"])

    def test_trace_refusals_on_room_and_read(self):
        w = self.w
        reply_task(w.ws[SIB], "$ask", tid="task-b2", room="!elsewhere:example.test", sender=SELF)
        self.assertIn("consult room", w.answer(SIB, "x", task_id="task-b2")["reason"])
        reply_task(w.ws[SIB], "$gone", tid="task-b3", sender=SELF)
        self.assertIn(f"last {policy.READ_LIMIT} messages", w.answer(SIB, "x", task_id="task-b3")["reason"])
        w.tr[SIB].read = lambda room, limit: {"ok": False, "reason": "network error"}
        self.assertIn("unreadable: network error", w.answer(SIB, "x", task_id="task-b1")["reason"])
        self.assertIn("unreadable", policy.thread_view(w.tr[SIB], ROOM, "$ask")["reason"])

    def test_an_origin_follow_up_needs_the_thread_in_sight(self):
        with mock.patch.object(policy, "thread_view", return_value={"ok": False, "reason": "not in sight"}):
            res = self.w.ask(SELF, SIB, cid=C3, task_id=TID)
        self.assertEqual((res["asked"], res["reason"]), (False, "not in sight"))

    def test_an_unwritable_pending_record_posts_nothing(self):
        before = len(self.w.room.msgs)
        with mock.patch.object(policy, "_write_record", side_effect=OSError("read-only")):
            res = self.w.ask(SIB, THIRD, cid=C2, via_task="task-b1")
        self.assertIn("pending record unwritable", res["reason"])
        self.assertEqual(len(self.w.room.msgs), before)

    def test_match_edges(self):
        w = self.w
        ws = w.ws[SELF]
        text = te.stamp_text(f"id: task-nosrc\nsource: ag2space\nchannel_id: {ROOM}\ntask: x\n", ws)
        (ws / "tasks" / "task-nosrc.txt").write_text(text, encoding="utf-8")
        self.assertIn("no source message", w.match(SELF, "task-nosrc")["reason"])
        w.tr[SIB].mention(SELF, final(C1, "up"), ROOM, reply_to="$ask", thread_root="$ask")
        w.deliver(SELF, "$ask2", "task-a2")
        self.assertIn("exactly one", policy.match_reply(w.tr[SELF], room=ROOM, self_mxid=SELF,
                                                         workspace=ws)["reason"])
        reply_task(ws, "$nowhere", tid="task-a9", sender=SIB)
        self.assertIn("not in the last", w.match(SELF, "task-a9")["reason"])
        with mock.patch.object(policy.os, "link", side_effect=FileExistsError):
            self.assertIn("already answered", w.match(SELF, "task-a2")["reason"])
        with mock.patch.object(policy.os, "link", side_effect=PermissionError("ro")):
            self.assertIn("could not be closed", w.match(SELF, "task-a2")["reason"])
        real = policy._write_record

        def fail_answered(path, record):
            if path.parent.name == "answered":
                raise OSError("ro")
            return real(path, record)
        with mock.patch.object(policy, "_write_record", side_effect=fail_answered):
            self.assertTrue(w.match(SELF, "task-a2")["matched"])

    def test_fresh_correlation_ids_are_well_formed(self):
        self.assertRegex(policy.new_cid(), r"^[0-9a-f]{16}$")


class TestReviewRoundFour(unittest.TestCase):
    """Regressions for the 84c0f9903 review: closed consults stay closed, a post whose record
    write failed is still matched, answers bind to the consult's own room and expire, closed
    ask tasks are inert, and a consult-room message is an origin only when the owner wrote it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def answered_by_sib(self, cid=C1, tids=("task-a2",)):
        self.w.tr[SIB].mention(SELF, final(cid, "up"), ROOM, reply_to="$ask", thread_root="$ask")
        for tid in tids:
            self.w.deliver(SELF, "$ask2", tid)

    def test_a_nudge_interleaved_with_a_match_cannot_reopen_the_consult(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.answered_by_sib(tids=("task-a2", "task-a3"))
        real, first = policy._read_record, []

        def match_lands_mid_nudge(path):
            rec = real(path)
            if rec is not None and path.parent.name == "pending" and not first:
                first.append(None)
                first[0] = w.match(SELF, "task-a2")["matched"]
            return rec
        with mock.patch.object(policy, "_read_record", side_effect=match_lands_mid_nudge):
            policy.mark_nudged(w.ws[SELF], C1)
        self.assertEqual(first, [True])
        self.assertEqual(pending_ids(w.ws[SELF]), [])
        again = w.match(SELF, "task-a3")
        self.assertFalse(again["matched"])
        self.assertIn("already answered", again["reason"])

    def test_a_stale_pending_copy_cannot_answer_twice(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.answered_by_sib(tids=("task-a2", "task-a3"))
        stale = (w.ws[SELF] / policy._STATE / "pending" / f"{C1}.json").read_text()
        self.assertTrue(w.match(SELF, "task-a2")["matched"])
        (w.ws[SELF] / policy._STATE / "pending" / f"{C1}.json").write_text(stale)
        self.assertIn("already answered", w.match(SELF, "task-a3")["reason"])

    def test_a_failed_record_write_after_the_post_is_recovered_from_the_room(self):
        w, real, calls = self.w, policy._write_record, []

        def second_write_fails(path, rec):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(path, rec)
        with mock.patch.object(policy, "_write_record", side_effect=second_write_fails):
            res = w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.assertEqual((res["asked"], res["ask_event"]), (True, "$ask"))
        self.assertIn("was not recorded", res["warning"])
        self.answered_by_sib()
        got = w.match(SELF, "task-a2")
        self.assertTrue(got["matched"], got)
        self.assertEqual((got["task_id"], got["root"]), (TID, "$ask"))

    def test_a_crash_between_post_and_record_is_recovered_from_the_room(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        path = w.ws[SELF] / policy._STATE / "pending" / f"{C1}.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "ask_event": None, "root": None}))
        self.answered_by_sib()
        self.assertTrue(w.match(SELF, "task-a2")["matched"])

    def test_an_answer_read_from_another_room_is_refused(self):
        w, other = self.w, "!other:example.test"
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.tr[SIB].mention(SELF, final(C1, "up"), ROOM, reply_to="$ask", thread_root="$ask")
        reply_task(w.ws[SELF], "$ask2", tid="task-a2", room=other, sender=SIB)
        res = policy.match_reply(w.tr[SELF], room=other, self_mxid=SELF, task_id="task-a2", workspace=w.ws[SELF])
        self.assertFalse(res["matched"])
        self.assertIn("not an answer to a pending consult", res["reason"])
        self.assertEqual(pending_ids(w.ws[SELF]), [C1])

    def test_an_expired_consult_is_refused_listed_and_pruned(self):
        w = self.w
        policy.consult(w.tr[SELF], room=ROOM, self_mxid=SELF, agent=SIB, question="q", workspace=w.ws[SELF],
                       task_id=TID, cid=C1, now=lambda: 1_000.0)
        self.answered_by_sib()
        res = policy.match_reply(w.tr[SELF], room=ROOM, self_mxid=SELF, task_id="task-a2",
                                 workspace=w.ws[SELF], expire_after_s=3600, now=1_000.0 + 3601)
        self.assertIn("expired", res["reason"])
        [row] = policy.pending(w.ws[SELF], 600, now=1_000.0 + 3601, expire_after_s=3600)
        self.assertEqual((row["expired"], row["nudge_due"]), (True, True))
        self.assertTrue(policy.match_reply(w.tr[SELF], room=ROOM, self_mxid=SELF, task_id="task-a2",
                                           workspace=w.ws[SELF], expire_after_s=3600, now=1_100.0)["matched"])
        policy.pending(w.ws[SELF], 600, now=1_000.0 + 3601, expire_after_s=3600)
        self.assertEqual(policy.records(w.ws[SELF], "answered"), [])

    def test_expiry_config_is_clamped(self):
        for raw, want in (("1", policy.EXPIRE_FLOOR_S), ("9e12", policy.EXPIRE_CEILING_S), ("x", policy.DEFAULT_EXPIRE_AFTER_S)):
            s = policy.settings(environ={policy.CONFIG_ROOM: ROOM, policy.CONFIG_EXPIRE_AFTER: raw}, manifest_cfg={})
            self.assertEqual(s["expire_after_s"], want, raw)

    def test_a_closed_ask_task_cannot_be_answered_or_consulted_from(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        (w.ws[SIB] / "results" / "task-b1.txt").write_text("[no-send]")
        self.assertIn("already has a result", w.ask(SIB, THIRD, cid=C2, via_task="task-b1")["reason"])
        self.assertIn("already has a result", w.answer(SIB, "x", task_id="task-b1")["reason"])
        self.assertEqual(w.tr[SIB].posted, [])

    def test_a_consult_room_message_is_an_origin_only_when_the_owner_wrote_it(self):
        w = self.w
        for tid, sender in (("task-s1", THIRD), ("task-o1", OWNER)):
            txt = te.stamp_text(f"id: {tid}\nsource: ag2space\nchannel_id: {ROOM}\nsource_message_id: $x\n"
                                f"user_id: {sender}\naccess_tier: owner\ntask: ask B\n", w.ws[SELF])
            (w.ws[SELF] / "tasks" / f"{tid}.txt").write_text(txt, encoding="utf-8")
        res = w.ask(SELF, SIB, cid=C1, task_id="task-s1")
        self.assertIn("must be the owner's own message", res["reason"])
        self.assertEqual(w.tr[SELF].posted, [])
        self.assertTrue(w.ask(SELF, SIB, cid=C2, task_id="task-o1")["asked"])

class TestLimitsCannotBeWidened(unittest.TestCase):
    """The thread window starts at the root event's server timestamp, never the marker's
    `since`, and each limit is the smaller of the marker's and the reader's own config."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)
        self.w.room.clock = lambda: T0

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_root_posted_by_hand_cannot_extend_the_window(self):
        w = self.w
        w.room.post(SELF, f"{SIB} — is the disk full?")
        w.deliver(SIB, "$ask", "task-b1")
        res = w.ask(SIB, THIRD, cid=C2, via_task="task-b1", now=lambda: T0 + 365 * 86400)
        self.assertEqual((res["asked"], res.get("limit_reached")), (False, True), res)
        self.assertIn(f"{policy.DEFAULT_MAX_DURATION_S}s window has passed", res["reason"])
        self.assertEqual(w.tr[SIB].posted, [])

    def test_a_slow_clock_on_the_first_asker_does_not_close_the_window(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID, now=lambda: T0 - 3600)
        w.deliver(SIB, "$ask", "task-b1")
        res = w.ask(SIB, THIRD, cid=C2, via_task="task-b1", now=lambda: T0 + 60)
        self.assertTrue(res["asked"], res)

    def test_a_root_can_narrow_the_limits_but_never_widen_them(self):
        root = {"ts": int(T0 * 1000)}
        wide = policy.effective_limits(root, {"since": 1, "max_s": 86400, "max_asks": 100}, 1800, 10)
        self.assertEqual(wide, {"since": T0, "max_s": 1800, "max_asks": 10})
        narrow = policy.effective_limits(root, {"since": 1, "max_s": 300, "max_asks": 2}, 1800, 10)
        self.assertEqual(narrow, {"since": T0, "max_s": 300, "max_asks": 2})
        self.assertEqual(policy.effective_limits({"ts": 1_760_000_000}, None, 1800, 10)["since"], 1_760_000_000.0)

    def test_the_ask_count_is_this_agents_own_asks_in_the_thread(self):
        w = self.w
        w.room.clock = time.time
        w.ask(SELF, SIB, cid=C1, task_id=TID, max_asks=100)
        w.ask(SELF, THIRD, cid=C3, task_id=TID, max_asks=100)  # SELF's asks do not count for SIB
        w.deliver(SIB, "$ask", "task-b1")
        self.assertTrue(w.ask(SIB, FOURTH, cid=C2, via_task="task-b1", max_asks=1)["asked"])
        res = w.ask(SIB, FOURTH, cid="4444444444444444", via_task="task-b1", max_asks=1)
        self.assertIn("1 of 1 asks", res["reason"])

    def test_a_root_without_a_server_timestamp_is_refused(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.room.msgs[0].pop("ts")
        w.deliver(SIB, "$ask", "task-b1")
        res = w.ask(SIB, THIRD, cid=C2, via_task="task-b1")
        self.assertIn("no server timestamp", res["reason"])
        self.assertIsNone(policy._ts_seconds(True))

    def test_the_window_ceiling_is_clamped(self):
        self.assertIsNotNone(policy.limit_reached({"since": 0, "max_s": 10**6, "max_asks": 5}, 1,
                                                  policy.MAX_DURATION_CEILING_S))
        self.assertIsNone(policy.limit_reached({"since": 0, "max_s": 10**6, "max_asks": 5}, 1,
                                               policy.MAX_DURATION_CEILING_S - 1))


class TestWindowOnServerTime(unittest.TestCase):
    """The window closes once true time since the root's server timestamp passes it: "now" is
    the largest of the newest server timestamp, the anchor's server timestamp plus local time
    elapsed since it, and the local clock. A clock provably behind the server is refused."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)
        self.w.room.clock = lambda: T0
        self.w.ask(SELF, SIB, cid=C1, task_id=TID, now=lambda: T0)

    def tearDown(self):
        self._tmp.cleanup()

    def refused(self, res):
        self.assertFalse(res["asked"], res)
        self.assertEqual(self.w.tr[SIB].posted, [])
        return res["reason"]

    def test_rui_1_a_slow_receiver_cannot_ask_past_the_window(self):
        skew = -3600
        self.w.deliver(SIB, "$ask", "task-b1", at=T0 + skew)
        reason = self.refused(self.w.ask(SIB, THIRD, cid=C2, via_task="task-b1", now=lambda: T0 + 1801 + skew))
        self.assertIn("behind the consult thread's newest server timestamp", reason)

    def test_a_receiver_slow_within_the_tolerance_is_still_held_to_the_window(self):
        skew = -100
        self.w.deliver(SIB, "$ask", "task-b1", at=T0 + skew)
        reason = self.refused(self.w.ask(SIB, THIRD, cid=C2, via_task="task-b1", now=lambda: T0 + 1801 + skew))
        self.assertIn("1800s window has passed", reason)

    def test_rui_2_a_fast_receiver_fails_closed(self):
        skew = 3600
        self.w.deliver(SIB, "$ask", "task-b1", at=T0 + skew)
        reason = self.refused(self.w.ask(SIB, THIRD, cid=C2, via_task="task-b1", now=lambda: T0 + 60 + skew))
        self.assertIn("1800s window has passed", reason)

    def test_an_ask_delivered_hours_late_is_measured_on_true_time(self):
        w = self.w
        w.room.clock = lambda: T0 + 60
        self.assertTrue(w.ask(SELF, SIB, cid=C3, task_id=TID, now=lambda: T0 + 60)["asked"])
        w.deliver(SIB, "$ask2", "task-b2", at=T0 + 6 * 3600)
        reason = self.refused(w.ask(SIB, THIRD, cid=C2, via_task="task-b2", now=lambda: T0 + 6 * 3600 + 30))
        self.assertIn("1800s window has passed", reason)

    def test_an_on_time_receiver_with_a_true_clock_is_allowed(self):
        self.w.deliver(SIB, "$ask", "task-b1", at=T0 + 5)
        self.assertTrue(self.w.ask(SIB, THIRD, cid=C2, via_task="task-b1", now=lambda: T0 + 65)["asked"])

    def test_the_first_askers_own_skew_fails_closed_both_ways(self):
        for skew, elapsed in ((3600, 60), (-3600, 1801)):
            with tempfile.TemporaryDirectory() as tmp:
                w = World(tmp)
                w.room.clock = lambda: T0
                w.ask(SELF, SIB, cid=C1, task_id=TID, now=lambda: T0 + skew)
                res = w.ask(SELF, SIB, cid=C2, task_id=TID, now=lambda: T0 + skew + elapsed)
                self.assertFalse(res["asked"], (skew, elapsed, res))

    def test_the_newest_server_timestamp_is_a_floor(self):
        self.w.deliver(SIB, "$ask", "task-b1", at=T0)
        self.w.room.clock = lambda: T0 + 1900
        self.w.room.post(OWNER, "later message in the thread", thread_root="$ask")
        reason = self.refused(self.w.ask(SIB, THIRD, cid=C2, via_task="task-b1", now=lambda: T0 + 1850))
        self.assertIn("window has passed", reason)

    def test_an_event_outside_the_thread_does_not_move_the_floor(self):
        self.w.deliver(SIB, "$ask", "task-b1", at=T0)
        self.w.room.clock = lambda: T0 + 3600
        self.w.room.post(OWNER, "an unrelated message stamped an hour ahead")
        self.assertTrue(self.w.ask(SIB, THIRD, cid=C2, via_task="task-b1", now=lambda: T0 + 60)["asked"])

    def test_disclosed_an_event_in_the_thread_stamped_ahead_refuses_every_ask(self):
        self.w.deliver(SIB, "$ask", "task-b1", at=T0)
        self.w.room.clock = lambda: T0 + 180
        self.w.room.post(OWNER, "a thread message stamped 180 s ahead", thread_root="$ask")
        reason = self.refused(self.w.ask(SIB, THIRD, cid=C2, via_task="task-b1", now=lambda: T0 + 30))
        self.assertIn("behind the consult thread's newest server timestamp", reason)

    def test_the_measure_takes_the_largest_term_and_refuses_a_slow_clock(self):
        tol = policy.CLOCK_TOLERANCE_S
        self.assertEqual(policy.measured_now(T0, (None, None), T0 + tol + 1), T0 + tol + 1)
        self.assertEqual(policy.measured_now(T0, (T0, T0 - 100), T0 - 70), T0 + 30)
        self.assertEqual(policy.measured_now(T0, (T0 - 60, T0 - 60), T0 + 5), T0 + 5)
        self.assertIsNone(policy.measured_now(T0, (T0, None), T0 - tol - 1))
        self.assertIsNone(policy.measured_now(None, (T0, T0), T0))

    def test_an_ask_refused_for_an_unmeasurable_clock_says_so(self):
        self.w.deliver(SIB, "$ask", "task-b1", at=T0)
        with mock.patch.object(policy, "measured_now", return_value=None):
            res = self.w.ask(SIB, THIRD, cid=C2, via_task="task-b1", now=lambda: T0)
        self.assertIn("cannot measure the thread window on server time", res["reason"])


class TestWindowResiduals(unittest.TestCase):
    """Documented residuals, asserting today's behaviour (SKILL.md "Time-window invariant"):
    the window is correct only within this agent's clock error plus any backward step, and the
    120 s check sees a slow clock only when the thread has a recent event. Each schedule here
    lets an ask through that a true clock would refuse."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)
        self.w.room.clock = lambda: T0
        self.w.ask(SELF, SIB, cid=C1, task_id=TID, now=lambda: T0)

    def tearDown(self):
        self._tmp.cleanup()

    def onward(self, arrived_local, now_local):
        self.w.deliver(SIB, "$ask", "task-b1", at=arrived_local)
        return self.w.ask(SIB, THIRD, cid=C2, via_task="task-b1", now=lambda: now_local)

    def test_residual_a_120s_backward_step_with_1801s_elapsed_is_allowed(self):
        self.assertTrue(self.onward(arrived_local=T0, now_local=T0 + 1801 - 120)["asked"])

    def test_residual_a_clock_1h_slow_handling_at_t_plus_4000_is_allowed(self):
        self.assertTrue(self.onward(arrived_local=T0 + 4000 - 3600, now_local=T0 + 4000 - 3600)["asked"])

    def test_residual_a_clock_120s_slow_delivered_at_t_plus_1801_is_allowed(self):
        self.assertTrue(self.onward(arrived_local=T0 + 1801 - 120, now_local=T0 + 1801 - 120)["asked"])


class TestKeweiRoundNine(unittest.TestCase):
    """Regressions for the 050d4b25c review: an incoming ask is admitted only within the thread's
    limits; ask publication is one recoverable state (no lost correlation, no second root); and
    a matched answer stays replayable for its reply task until that task has a result."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def hand_ask(self, question="and the disk?"):
        self.w.room.post(SELF, f"{SIB} — {question}", thread_root="$ask")
        return self.w.room.msgs[-1]["event_id"]

    # 1: the thread cap is enforced on the ask being consumed
    def test_an_ask_posted_after_the_window_is_not_answered_but_one_inside_it_unwinds_late(self):
        w = self.w
        w.room.clock = lambda: T0
        w.ask(SELF, SIB, cid=C1, task_id=TID, max_duration_s=60, now=lambda: T0)
        w.room.clock = lambda: T0 + 61
        ev = self.hand_ask()
        w.deliver(SIB, ev, "task-b1", at=T0 + 61)
        late = w.answer(SIB, "x", task_id="task-b1", max_duration_s=60)
        self.assertFalse(late["answered"])
        self.assertIn("60s window has passed", late["reason"])
        self.assertIn("outside the thread's window", late["reason"])
        w.deliver(SIB, "$ask", "task-b0", at=T0)
        self.assertTrue(w.answer(SIB, "the root ask, answered after the deadline", task_id="task-b0",
                                 max_duration_s=60)["answered"])

    # 2: ask publication is one recoverable state
    def test_a_failed_final_write_does_not_start_a_second_thread(self):
        w, real, n = self.w, policy._write_record, {"i": 0}

        def second_fails(path, rec):
            n["i"] += 1
            if n["i"] == 2:
                raise OSError("disk full")
            return real(path, rec)
        with mock.patch.object(policy, "_write_record", side_effect=second_fails):
            self.assertTrue(w.ask(SELF, SIB, cid=C1, task_id=TID)["asked"])
        again = w.ask(SELF, SIB, cid=C2, task_id=TID)
        self.assertEqual((again["asked"], again["root"], again["follow_up"]), (True, "$ask", True), again)
        self.assertEqual(w.tr[SELF].thread_roots, [None, "$ask"])

    def test_a_crash_between_post_and_record_does_not_start_a_second_thread(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        path = w.ws[SELF] / policy._STATE / "pending" / f"{C1}.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "ask_event": None, "root": None}))
        again = w.ask(SELF, SIB, cid=C2, task_id=TID)
        self.assertEqual((again["root"], again["follow_up"]), ("$ask", True), again)
        self.assertEqual(json.loads(path.read_text())["ask_event"], "$ask")

    def test_an_unconfirmed_post_keeps_its_record_and_its_reply_matches(self):
        w = self.w
        tr = w.tr[SELF]
        real = tr.mention
        tr.mention = lambda *a, **k: (real(*a, **k), {"ok": True, "event_id": None})[1]
        res = w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.assertEqual((res["asked"], res["unconfirmed"]), (True, True))
        self.assertEqual(pending_ids(w.ws[SELF]), [C1])
        w.tr[SIB].mention(SELF, final(C1, "up"), ROOM, reply_to="$ask", thread_root="$ask")
        w.deliver(SELF, "$ask2", "task-a2")
        self.assertTrue(w.match(SELF, "task-a2")["matched"])

    def test_an_unconfirmed_post_beyond_the_read_refuses_a_new_root(self):
        w = self.w
        w.tr[SELF].mention = lambda *a, **k: {"ok": True, "event_id": None}
        self.assertTrue(w.ask(SELF, SIB, cid=C1, task_id=TID)["unconfirmed"])
        w.tr[SELF].mention = FakeTransport.mention.__get__(w.tr[SELF])
        w.room.post(OWNER, "filler one")
        w.room.post(OWNER, "filler two")
        with mock.patch.object(policy, "READ_LIMIT", 2):  # the read no longer covers the whole room
            res = w.ask(SELF, SIB, cid=C2, task_id=TID)
        self.assertFalse(res["asked"])
        self.assertIn("unconfirmed", res["reason"])
        self.assertEqual(len(w.room.msgs), 2)
        self.assertIn(C1, [r["cid"] for r in policy.pending(w.ws[SELF], 600) if r["unconfirmed"]])

    def test_an_unconfirmed_post_absent_from_an_incomplete_read_stays_unconfirmed(self):
        w = self.w
        w.tr[SELF].mention = lambda *a, **k: {"ok": True, "event_id": None}
        self.assertTrue(w.ask(SELF, SIB, cid=C1, task_id=TID)["unconfirmed"])
        w.tr[SELF].mention = FakeTransport.mention.__get__(w.tr[SELF])
        w.tr[SELF].read = lambda room, limit: {"ok": True, "messages": [], "complete": False}  # the real adapter
        age_record(w.ws[SELF], C1, 86400)  # a day old: well past any landing delay, inside the expiry
        res = w.ask(SELF, SIB, cid=C2, task_id=TID)
        self.assertFalse(res["asked"], res)
        self.assertIn("unconfirmed", res["reason"])
        self.assertIn("owner can ask again", res["reason"])
        self.assertEqual((pending_ids(w.ws[SELF]), policy._root_claim(w.ws[SELF], TID).exists()), ([C1], True))
        self.assertEqual(w.room.msgs, [])
        age_record(w.ws[SELF], C1, 8 * 86400)  # past expiry: still unconfirmed, still blocking
        self.assertFalse(w.ask(SELF, SIB, cid=C2, task_id=TID)["asked"])
        (w.ws[SELF] / "tasks" / "task-2.txt").write_text(te.stamp_text(task_text(tid="task-2"), w.ws[SELF]))
        self.assertTrue(w.ask(SELF, SIB, cid=C2, task_id="task-2")["asked"])  # the owner asks again

    def test_only_one_ask_from_a_task_can_claim_its_root(self):
        self.assertTrue(policy.claim_root(self.w.ws[SELF], TID, C1))
        self.assertFalse(policy.claim_root(self.w.ws[SELF], TID, C2))
        with mock.patch.object(policy, "reconcile", return_value=None):  # a racing ask read first
            res = self.w.ask(SELF, SIB, cid=C2, task_id=TID)
        self.assertIn("holds its thread root", res["reason"])
        self.assertFalse(res["asked"])
        self.assertEqual(self.w.room.msgs, [])

    def test_a_refused_post_releases_the_root_claim(self):
        w = self.w
        w.tr[SELF].mention = lambda *a, **k: {"ok": False, "reason": "gate denied", "posted": False}
        self.assertFalse(w.ask(SELF, SIB, cid=C1, task_id=TID)["asked"])
        w.tr[SELF].mention = FakeTransport.mention.__get__(w.tr[SELF])
        self.assertTrue(w.ask(SELF, SIB, cid=C2, task_id=TID)["asked"])

    def test_an_empty_root_claim_is_never_released(self):
        claim = policy._root_claim(self.w.ws[SELF], TID)
        claim.parent.mkdir(parents=True, exist_ok=True)
        claim.write_text("")  # a crash between creating the claim and writing it (older code)
        self.assertFalse(policy.claim_root(self.w.ws[SELF], TID, C1))
        (self.w.ws[SELF] / "results" / f"{TID}.txt").write_text("done")
        policy.pending(self.w.ws[SELF], 600)
        self.assertTrue(claim.exists())

    def test_a_crash_while_writing_a_claim_leaves_no_claim(self):
        with mock.patch.object(policy.os, "link", side_effect=OSError("crash")), self.assertRaises(OSError):
            policy.claim_root(self.w.ws[SELF], TID, C1)
        self.assertFalse(policy._root_claim(self.w.ws[SELF], TID).exists())
        self.assertEqual(list(policy._root_claim(self.w.ws[SELF], TID).parent.iterdir()), [])
        self.assertTrue(policy.claim_root(self.w.ws[SELF], TID, C1))

    def test_root_claims_of_gone_tasks_stay_and_leave_the_listing(self):
        policy.claim_root(self.w.ws[SELF], TID, C1)
        (self.w.ws[SELF] / "tasks" / f"{TID}.txt").unlink()  # archived away
        self.assertEqual(policy.pending(self.w.ws[SELF], 600), [])
        self.assertTrue(policy._root_claim(self.w.ws[SELF], TID).exists())

    # 3: a matched answer is replayable until its reply task has a result
    def test_a_crash_after_the_claim_replays_the_answer_to_the_same_reply_task(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.tr[SIB].mention(SELF, final(C1, "up"), ROOM, reply_to="$ask", thread_root="$ask")
        w.deliver(SELF, "$ask2", "task-a2")
        w.deliver(SELF, "$ask2", "task-a3")
        real_link = os.link

        class Crash(Exception):
            pass

        def link_then_crash(src, dst):
            real_link(src, dst)
            raise Crash()
        with mock.patch.object(policy.os, "link", side_effect=link_then_crash), self.assertRaises(Crash):
            w.match(SELF, "task-a2")
        again = w.match(SELF, "task-a2")
        self.assertEqual((again["matched"], again.get("replay"), again["reply_text"], again["task_id"]),
                         (True, True, "up", TID), again)
        self.assertIn("already answered", w.match(SELF, "task-a3")["reason"])
        (w.ws[SELF] / "results" / "task-a2.txt").write_text("[no-send]")
        self.assertIn("already answered", w.match(SELF, "task-a2")["reason"])


def age_record(ws, cid, seconds):
    path = Path(ws) / policy._STATE / "pending" / f"{cid}.json"
    rec = json.loads(path.read_text())
    path.write_text(json.dumps({**rec, "asked_at": rec["asked_at"] - seconds}))


def age_answer_claim(ws, ask_event, seconds):
    path = policy._answer_claim(ws, ask_event)
    rec = json.loads(path.read_text())
    path.write_text(json.dumps({**rec, "at": rec["at"] - seconds}))


class TestPublicationIsOneRecoverableState(unittest.TestCase):
    """Every post (an ask or an answer) is claimed before it is sent and settled after: a refusal
    the transport reports as made before sending releases the claim; anything ambiguous keeps it
    and is reconciled from the room by consult id, so nothing is posted twice."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def landed_then(self, tr, result):
        real = tr.mention

        def post(*a, **k):
            real(*a, **k)
            return result
        tr.mention = post
        return real

    def test_a_timeout_after_the_post_landed_does_not_start_a_second_thread(self):
        w = self.w
        real = self.landed_then(w.tr[SELF], {"ok": False, "reason": "room transport failed: timed out"})
        first = w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.assertEqual((first["asked"], first["unconfirmed"]), (True, True), first)
        w.tr[SELF].mention = real
        again = w.ask(SELF, SIB, cid=C2, task_id=TID)
        self.assertEqual((again["root"], again["follow_up"]), ("$ask", True), again)
        self.assertEqual([m.get("thread_root") for m in w.room.msgs], [None, "$ask"])

    def test_the_room_cli_reports_timeouts_and_garbled_output_as_ambiguous(self):
        with tempfile.NamedTemporaryFile(suffix=".py") as f:
            def boom(argv, **kw):
                raise subprocess.TimeoutExpired(argv, 60)
            timed_out = cli.RoomCliTransport(f.name, SELF, runner=boom).mention(SIB, "q", ROOM)
            garbled = cli.RoomCliTransport(f.name, SELF, runner=lambda argv, **kw:
                                           subprocess.CompletedProcess(argv, 0, stdout="half", stderr="")
                                           ).mention(SIB, "q", ROOM)
        for res in (timed_out, garbled):
            self.assertFalse(policy._sent_before_publication(res), res)
        self.assertTrue(policy._sent_before_publication({"ok": False, "posted": False}))

    def test_room_ops_mention_marks_its_pre_send_refusals(self):
        sys.path.insert(0, str(ROOM_OPS_CLI.parent))
        mention = _load("room_ops_mention", ROOM_OPS_CLI.parent / "mention.py")
        self.assertIs(mention.mention("peer", "x", "")["posted"], False)
        self.assertIs(mention.mention("peer", "x", ROOM, reply_to="not-an-event")["posted"], False)

    def chain_to_up_answer(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        w.ask(SIB, THIRD, cid=C2, via_task="task-b1")
        w.deliver(THIRD, "$ask2", "task-c1")
        w.answer(THIRD, "disk is 80% full", task_id="task-c1")
        w.deliver(SIB, "$ask3", "task-b2")
        up = w.match(SIB, "task-b2")
        self.assertTrue(up["matched"], up)
        return up

    def test_a_replayed_match_does_not_post_the_up_chain_answer_twice(self):
        w = self.w
        up = self.chain_to_up_answer()
        first = w.answer(SIB, "per third: 80%", up=up["answer_up"]["up"])
        replayed = w.match(SIB, "task-b2")  # B crashed before writing task-b2's result
        self.assertTrue(replayed.get("replay"), replayed)
        second = w.answer(SIB, "per third: 80%", up=replayed["answer_up"]["up"])
        self.assertEqual((second["answered"], second.get("replay"), second["event_id"]),
                         (True, True, first["event_id"]), second)
        self.assertEqual(sum(1 for m in w.room.msgs if m["sender"] == SIB and m["body"].startswith(SELF)), 1)

    def test_answering_the_same_ask_twice_posts_once(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        one = w.answer(SIB, "up", task_id="task-b1")
        two = w.answer(SIB, "up", task_id="task-b1")
        self.assertEqual((two.get("replay"), two["event_id"]), (True, one["event_id"]))
        self.assertEqual(len(w.room.msgs), 2)

    def test_an_answer_that_timed_out_after_landing_is_found_not_reposted(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        real = self.landed_then(w.tr[SIB], {"ok": False, "reason": "room transport failed: timed out"})
        self.assertTrue(w.answer(SIB, "up", task_id="task-b1")["unconfirmed"])
        w.tr[SIB].mention = real
        again = w.answer(SIB, "up", task_id="task-b1")
        self.assertEqual((again.get("replay"), again["event_id"]), (True, "$ask2"), again)
        self.assertEqual(len(w.room.msgs), 2)

    def test_an_answer_that_never_landed_stays_unconfirmed(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        w.tr[SIB].mention = lambda *a, **k: {"ok": False, "reason": "room transport failed: timed out"}
        self.assertTrue(w.answer(SIB, "up", task_id="task-b1")["unconfirmed"])
        w.tr[SIB].mention = FakeTransport.mention.__get__(w.tr[SIB])
        for age in (86400, 8 * 86400):
            age_answer_claim(w.ws[SIB], "$ask", age)
            self.assertIn("unconfirmed", w.answer(SIB, "up", task_id="task-b1")["reason"])
        self.assertEqual(len(w.room.msgs), 1)

    def test_an_answer_that_timed_out_and_lands_late_is_not_posted_twice(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        w.tr[SIB].mention = lambda *a, **k: {"ok": False, "reason": "room transport failed: timed out"}
        self.assertTrue(w.answer(SIB, "up", task_id="task-b1")["unconfirmed"])
        w.tr[SIB].mention = FakeTransport.mention.__get__(w.tr[SIB])
        early = w.answer(SIB, "up", task_id="task-b1")  # 1 s later, the whole room shows nothing yet
        self.assertIn("earlier answer to this ask is unconfirmed", early["reason"])
        w.room.post(SIB, f"{SELF} — {final(C1, 'up')}", reply_to="$ask", thread_root="$ask")  # it lands late
        again = w.answer(SIB, "up", task_id="task-b1")
        self.assertEqual((again.get("replay"), again["event_id"]), (True, "$ask2"), again)
        self.assertEqual(len(w.room.msgs), 2)

    def test_an_answer_unconfirmed_is_not_posted_again(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        w.tr[SIB].mention = lambda *a, **k: {"ok": False, "reason": "room transport failed: timed out"}
        w.answer(SIB, "up", task_id="task-b1")
        w.tr[SIB].mention = FakeTransport.mention.__get__(w.tr[SIB])
        res = w.answer(SIB, "up", task_id="task-b1")
        self.assertIn("earlier answer to this ask is unconfirmed", res["reason"])
        self.assertEqual(len(w.room.msgs), 1)

    def test_a_paused_publisher_is_never_displaced_into_a_second_root(self):
        w, real, seen = self.w, policy._write_record, []

        def pause_before_record(path, rec):
            if not seen and path.parent.name == "pending":
                seen.append(1)  # A holds the claim and has not written its record: B runs now
                claim = policy._root_claim(w.ws[SELF], TID)
                old = time.time() - 3600
                os.utime(claim, (old, old))
                seen.append(w.ask(SELF, SIB, cid=C2, task_id=TID))
            return real(path, rec)
        with mock.patch.object(policy, "_write_record", side_effect=pause_before_record):
            a = w.ask(SELF, SIB, cid=C1, task_id=TID)
        b = seen[1]
        self.assertFalse(b["asked"], b)
        self.assertIn("holds its thread root", b["reason"])
        self.assertTrue(a["asked"], a)
        self.assertEqual([m.get("thread_root") for m in w.room.msgs], [None])

    def test_the_owner_can_ask_again_with_a_new_task(self):
        w = self.w
        policy.claim_root(w.ws[SELF], TID, C1)  # a claim with no record: this task is blocked
        self.assertFalse(w.ask(SELF, SIB, cid=C2, task_id=TID)["asked"])
        (w.ws[SELF] / "tasks" / "task-2.txt").write_text(te.stamp_text(task_text(tid="task-2"), w.ws[SELF]))
        self.assertTrue(w.ask(SELF, SIB, cid=C3, task_id="task-2")["asked"])

    def test_an_orphan_claim_is_listed_and_held_while_its_task_is_live(self):
        claim = policy._root_claim(self.w.ws[SELF], TID)
        policy.claim_root(self.w.ws[SELF], TID, C1)
        [row] = policy.pending(self.w.ws[SELF], 600, now=time.time() + 30 * 86400)
        self.assertEqual((row["task_id"], row["cid"], row["claim_only"]), (TID, C1, True))
        self.assertTrue(claim.exists())

    def test_reconcile_and_claim_edges(self):
        w = self.w
        w.tr[SELF].mention = lambda *a, **k: {"ok": True, "event_id": None}
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.tr[SELF].mention = FakeTransport.mention.__get__(w.tr[SELF])
        w.tr[SELF].room.post(SELF, f"{SIB} — is the disk full?")
        with mock.patch.object(policy, "_write_record", side_effect=OSError("ro")):
            self.assertIn("unconfirmed", policy.reconcile(w.tr[SELF], ROOM, SELF, w.ws[SELF], task_id=TID))
        w.tr[SELF].read = lambda room, limit: {"ok": False, "reason": "down"}
        self.assertEqual(policy._room_messages(w.tr[SELF], ROOM), [])
        self.assertIn("unconfirmed", policy.reconcile(w.tr[SELF], ROOM, SELF, w.ws[SELF], task_id=TID,
                                                      now=time.time() + 10 * 86400))
        self.assertEqual(pending_ids(w.ws[SELF]), [C1])

    def test_answer_claim_edges(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        with mock.patch.object(policy, "_claim_file", return_value=False):
            self.assertIn("being posted", w.answer(SIB, "up", task_id="task-b1")["reason"])
        with mock.patch.object(policy, "_write_record", side_effect=OSError("ro")):
            self.assertTrue(w.answer(SIB, "up", task_id="task-b1")["answered"])
        answers = w.ws[SIB] / policy._STATE / "answers"
        roots = w.ws[SELF] / policy._STATE / "roots"
        (roots / "task-junk.json").write_text("not json")
        (w.ws[SIB] / "results" / "task-b1.txt").write_text("[no-send]")
        later = time.time() + 30 * 86400
        policy.pending(w.ws[SIB], 600, now=later)
        listed = [r.get("task_id") for r in policy.pending(w.ws[SELF], 600, now=later)]
        self.assertNotIn("task-junk", listed)  # a claim whose task is gone is not listed
        self.assertEqual(len(list(answers.glob("*.json"))), 1)
        self.assertTrue((roots / "task-junk.json").exists())

    def test_a_root_ask_that_timed_out_and_lands_late_does_not_start_a_second_thread(self):
        w = self.w
        w.tr[SELF].mention = lambda *a, **k: {"ok": False, "reason": "room transport failed: timed out"}
        self.assertTrue(w.ask(SELF, SIB, cid=C1, task_id=TID)["unconfirmed"])
        w.tr[SELF].mention = FakeTransport.mention.__get__(w.tr[SELF])
        early = w.ask(SELF, SIB, cid=C2, task_id=TID)  # 1 s later, the whole room shows nothing yet
        self.assertFalse(early["asked"], early)
        self.assertIn("unconfirmed", early["reason"])
        self.assertEqual(w.room.msgs, [])
        w.room.post(SELF, f"{SIB} — is the disk full?")  # the timed-out post lands
        again = w.ask(SELF, SIB, cid=C2, task_id=TID)
        self.assertEqual((again["asked"], again["root"], again["follow_up"]), (True, "$ask", True), again)
        self.assertEqual([m.get("thread_root") for m in w.room.msgs], [None, "$ask"])

    def test_an_unconfirmed_onward_ask_is_reconciled_before_a_follow_up(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        real = self.landed_then(w.tr[SIB], {"ok": False, "reason": "room transport failed: timed out"})
        self.assertTrue(w.ask(SIB, THIRD, cid=C2, via_task="task-b1")["unconfirmed"])
        w.tr[SIB].mention = real
        again = w.ask(SIB, THIRD, cid=C3, via_task="task-b1")
        self.assertEqual((again["asked"], again["follow_up"]), (True, True), again)
        [first] = [r for r in policy.records(w.ws[SIB], "pending") if r["cid"] == C2]
        self.assertEqual(first["ask_event"], "$ask2")


class TestOneRootPerTaskEver(unittest.TestCase):
    """A task gets at most one root, ever: a root or answer claim is never released while a task
    that could post from it is live (in tasks/ with no result). Expiry only ages records out of
    the pending listing; it never frees a claim."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_kewei_1_a_landed_unconfirmed_root_out_of_the_read_is_never_reposted_after_expiry(self):
        w = self.w
        real = w.tr[SELF].mention

        def landed_without_receipt(*a, **k):
            real(*a, **k)
            return {"ok": True, "event_id": None}
        w.tr[SELF].mention = landed_without_receipt
        self.assertTrue(w.ask(SELF, SIB, cid=C1, task_id=TID)["unconfirmed"])
        w.tr[SELF].mention = real
        w.room.post(OWNER, "later message one")
        w.room.post(OWNER, "later message two")
        age_record(w.ws[SELF], C1, 8 * 86400)
        policy.pending(w.ws[SELF], 600, now=time.time())
        with mock.patch.object(policy, "READ_LIMIT", 2):  # the first ask has scrolled out of the read
            res = w.ask(SELF, SIB, cid=C2, task_id=TID)
        self.assertFalse(res["asked"], res)
        self.assertEqual(len([m for m in w.room.msgs if m.get("sender") == SELF and not m.get("thread_root")]), 1)

    def test_kewei_2_expiry_cleanup_never_displaces_a_paused_publisher(self):
        w, real, seen = self.w, policy._write_record, []

        def pause_before_record(path, rec):
            if not seen and path.parent.name == "pending":
                seen.append(1)  # A holds the claim, has not written its record; time passes
                claim = policy._root_claim(w.ws[SELF], TID)
                data = json.loads(claim.read_text())
                claim.write_text(json.dumps({**data, "asked_at": data["asked_at"] - 30 * 86400}))
                old = time.time() - 30 * 86400
                os.utime(claim, (old, old))
                policy.pending(w.ws[SELF], 600)  # an expiry cleanup pass runs
                seen.append(w.ask(SELF, SIB, cid=C2, task_id=TID))
            return real(path, rec)
        with mock.patch.object(policy, "_write_record", side_effect=pause_before_record):
            a = w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.assertFalse(seen[1]["asked"], seen[1])
        self.assertTrue(a["asked"], a)
        self.assertEqual([m.get("thread_root") for m in w.room.msgs], [None])

    def test_a_landed_unconfirmed_answer_is_never_reposted_after_expiry(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        real = w.tr[SIB].mention

        def landed_without_receipt(*a, **k):
            real(*a, **k)
            return {"ok": False, "reason": "room transport failed: timed out"}
        w.tr[SIB].mention = landed_without_receipt
        self.assertTrue(w.answer(SIB, "up", task_id="task-b1")["unconfirmed"])
        w.tr[SIB].mention = real
        age_answer_claim(w.ws[SIB], "$ask", 8 * 86400)
        policy.pending(w.ws[SIB], 600)
        with mock.patch.object(policy, "_room_messages", return_value=[]):  # the answer is beyond the read
            res = w.answer(SIB, "up", task_id="task-b1")
        self.assertIn("unconfirmed", res["reason"])
        self.assertEqual(len(w.room.msgs), 2)

    def test_a_live_claim_only_task_is_listed(self):
        w = self.w
        policy.claim_root(w.ws[SELF], TID, C1)
        later = time.time() + 30 * 86400
        self.assertEqual([r["claim_only"] for r in policy.pending(w.ws[SELF], 600, now=later)], [True])

    def test_answer_claims_are_never_pruned(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        w.answer(SIB, "up", task_id="task-b1")
        (w.ws[SIB] / "results" / "task-b1.txt").write_text("[no-send]")
        policy.pending(w.ws[SIB], 600, now=time.time() + 30 * 86400)
        self.assertTrue(policy._answer_claim(w.ws[SIB], "$ask").exists())

    def test_an_answer_claim_remembers_every_task_that_posted_through_it(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        w.answer(SIB, "up", task_id="task-b1")
        claim = policy._answer_claim(w.ws[SIB], "$ask")
        policy._note_tasks(claim, json.loads(claim.read_text()), ["task-b9", None, "task-b1"])
        self.assertEqual(json.loads(claim.read_text())["tasks"], ["task-b1", "task-b9"])
        with mock.patch.object(policy, "_write_record", side_effect=OSError("ro")):
            policy._note_tasks(claim, json.loads(claim.read_text()), ["task-b10"])
        self.assertFalse(policy.task_can_post(w.ws[SIB], "not a task id"))

    def test_an_expired_unconfirmed_record_leaves_the_listing_but_still_blocks_the_task(self):
        w = self.w
        w.tr[SELF].mention = lambda *a, **k: {"ok": True, "event_id": None}
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.tr[SELF].mention = FakeTransport.mention.__get__(w.tr[SELF])
        policy.mark_nudged(w.ws[SELF], C1)
        later = time.time() + 30 * 86400
        listed = policy.pending(w.ws[SELF], 600, now=later)
        self.assertEqual([r.get("claim_only") for r in listed], [None])
        self.assertTrue(listed[0]["expired"])
        self.assertFalse(w.ask(SELF, SIB, cid=C2, task_id=TID)["asked"])


class TestClaimsAreNeverReleased(unittest.TestCase):
    """Root and answer claims are never pruned, so no cleanup can race an admitted publisher."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_kewei_3_cleanup_cannot_let_two_admitted_callers_post_two_roots(self):
        w, real, out = self.w, policy._write_record, []
        admitted = policy.trusted_task(TID, w.ws[SELF])  # B passed the task gate before the result

        def a_paused_before_its_record(path, rec):
            if not out and path.parent.name == "pending":
                out.append(1)
                (w.ws[SELF] / "results" / f"{TID}.txt").write_text("done")
                policy.pending(w.ws[SELF], 600)  # a cleanup pass
                with mock.patch.object(policy, "trusted_task", return_value=admitted):
                    out.append(w.ask(SELF, SIB, cid=C2, task_id=TID))
            return real(path, rec)
        with mock.patch.object(policy, "_write_record", side_effect=a_paused_before_its_record):
            a = w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.assertFalse(out[1]["asked"], out[1])
        self.assertTrue(a["asked"], a)
        self.assertEqual([m.get("thread_root") for m in w.room.msgs], [None])
        self.assertTrue(policy._root_claim(w.ws[SELF], TID).exists())

    def test_kewei_4_an_onward_answer_is_never_posted_twice_from_the_live_original_task(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.deliver(SIB, "$ask", "task-b1")
        w.ask(SIB, THIRD, cid=C2, via_task="task-b1")
        w.deliver(THIRD, "$ask2", "task-c1")
        w.answer(THIRD, "disk is 80% full", task_id="task-c1")
        w.deliver(SIB, "$ask3", "task-b2")
        up = w.match(SIB, "task-b2")
        first = w.answer(SIB, "per third: 80%", up=up["answer_up"]["up"])
        recorded = json.loads(policy._answer_claim(w.ws[SIB], "$ask").read_text())["tasks"]
        self.assertEqual(set(recorded), {"task-b1", "task-b2"})  # the original ask task too
        (w.ws[SIB] / "results" / "task-b2.txt").write_text("[no-send]")
        policy.pending(w.ws[SIB], 600)
        second = w.answer(SIB, "per third: 80%", task_id="task-b1")  # task-b1 is still live
        self.assertEqual((second.get("replay"), second["event_id"]), (True, first["event_id"]), second)
        self.assertEqual(sum(1 for m in w.room.msgs if m["sender"] == SIB and m["body"].startswith(SELF)), 1)
        tasks = json.loads(policy._answer_claim(w.ws[SIB], "$ask").read_text())["tasks"]
        self.assertEqual(set(tasks), {"task-b1", "task-b2"})

    def test_claims_of_finished_tasks_stay_on_disk_and_leave_the_listing(self):
        w = self.w
        policy.claim_root(w.ws[SELF], TID, C1)
        self.assertEqual([r.get("claim_only") for r in policy.pending(w.ws[SELF], 600)], [True])
        (w.ws[SELF] / "results" / f"{TID}.txt").write_text("done")
        self.assertEqual(policy.pending(w.ws[SELF], 600), [])
        self.assertTrue(policy._root_claim(w.ws[SELF], TID).exists())


class TestPlainMessages(unittest.TestCase):
    """An ask is the question alone, mentioning the agent; the correlation lives in this
    agent's own records, keyed by thread root and ask event."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_ask_is_the_question_alone_and_roots_a_new_thread(self):
        w = self.w
        res = w.ask(SELF, SIB, cid=C1, task_id=TID, question="Is the build host up?")
        self.assertEqual((res["asked"], res["root"], res["ask_event"]), (True, "$ask", "$ask"), res)
        self.assertEqual(w.tr[SELF].posted, [(SIB, "Is the build host up?", ROOM)])
        self.assertEqual(w.tr[SELF].thread_roots, [None])
        self.assertEqual(w.room.msgs[0]["body"], f"{SIB} — Is the build host up?")

    def test_the_record_holds_the_correlation_and_no_message_carries_it(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID, question="Is the build host up?")
        [rec] = policy.records(w.ws[SELF], "pending")
        self.assertEqual((rec["root"], rec["ask_event"], rec["agent"], rec["body"]),
                         ("$ask", "$ask", SIB, "Is the build host up?"))
        for m in w.room.msgs:
            self.assertNotIn(C1, m["body"])
            self.assertNotIn("owner-agent-consult", m["body"])

    def test_the_skill_ships_no_marker_parser(self):
        for name in ("MARKER", "parse_ask", "ask_line", "answer_tag"):
            self.assertFalse(hasattr(policy, name), name)


class TestAnswersByRelation(unittest.TestCase):
    """An answer is any message from the asked agent in the consult thread after the ask, or
    a reply citing the ask. What reads as progress is judged by the agent, not by text rules."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)
        self.w.ask(SELF, SIB, cid=C1, task_id=TID, question="Is the build host up?")

    def tearDown(self):
        self._tmp.cleanup()

    def reply(self, body, *, sender=SIB, tid="task-a2", **rel):
        rel = rel or {"thread_root": "$ask"}
        self.w.room.post(sender, body, **rel)
        event = self.w.room.msgs[-1]["event_id"]
        self.w.deliver(SELF, event, tid)
        return event

    def test_any_plain_message_from_the_asked_agent_in_the_thread_answers(self):
        self.reply("Yes, it has been up since 09:00.")
        got = self.w.match(SELF, "task-a2")
        self.assertEqual((got["matched"], got["task_id"], got["reply_text"]),
                         (True, TID, "Yes, it has been up since 09:00."), got)
        self.assertEqual(got["lead"], f"[channel: {OWNER_DM}]\n")

    def test_a_reply_citing_the_ask_outside_the_thread_answers(self):
        self.reply("Yes.", reply_to="$ask")
        self.assertTrue(self.w.match(SELF, "task-a2")["matched"])

    def test_a_message_judged_not_final_leaves_the_consult_open_for_the_next_one(self):
        self.reply("On it, checking now.", tid="task-a2")
        held = policy.match_reply(self.w.tr[SELF], room=ROOM, self_mxid=SELF, task_id="task-a2",
                                  workspace=self.w.ws[SELF], not_final=True)
        self.assertEqual((held["matched"], held["progress"]), (False, True), held)
        self.assertEqual(pending_ids(self.w.ws[SELF]), [C1])
        self.reply("Yes, it is up.", tid="task-a3")
        got = self.w.match(SELF, "task-a3")
        self.assertEqual((got["matched"], got["reply_text"]), (True, "Yes, it is up."), got)
        self.assertFalse(self.w.match(SELF, "task-a2")["matched"])

    def test_messages_that_are_not_this_consults_answer_do_not_match(self):
        self.reply("Yes.", sender=OWNER, tid="task-o")
        self.reply("Yes.", tid="task-x", thread_root="$elsewhere")
        for tid in ("task-o", "task-x"):
            got = self.w.match(SELF, tid)
            self.assertFalse(got["matched"], tid)
            self.assertIn("not an answer to a pending consult", got["reason"])
        self.assertEqual(pending_ids(self.w.ws[SELF]), [C1])

    def test_a_message_from_before_the_ask_does_not_answer_it(self):
        w = self.w
        w.room.post(SIB, "unrelated, before the follow-up", thread_root="$ask")
        early = w.room.msgs[-1]["event_id"]
        w.deliver(SELF, early, "task-a2")
        self.assertTrue(w.match(SELF, "task-a2")["matched"])  # it answers the first ask
        w.ask(SELF, SIB, cid=C2, task_id=TID, question="And the disk?")
        w.deliver(SELF, early, "task-a3")
        self.assertIn("already answered", w.match(SELF, "task-a3")["reason"])
        self.assertEqual(pending_ids(w.ws[SELF]), [C2])

    def test_a_reply_citing_a_follow_up_answers_that_follow_up(self):
        w = self.w
        follow = w.ask(SELF, SIB, cid=C2, task_id=TID, question="And the disk?")
        self.reply("Disk is 80% full.", thread_root="$ask", reply_to=follow["ask_event"])
        got = w.match(SELF, "task-a2")
        self.assertEqual((got["cid"], got["reply_text"]), (C2, "Disk is 80% full."), got)

    def test_an_answer_found_by_pending_matches_by_event_and_replays_until_relayed(self):
        w = self.w
        w.room.post(SIB, "Yes.", thread_root="$ask")  # no task arrived for it
        waiting = policy.waiting_answers(w.tr[SELF], ROOM, w.ws[SELF])
        self.assertEqual(waiting[C1]["event_id"], "$ask2")
        first = policy.match_reply(w.tr[SELF], room=ROOM, self_mxid=SELF, event_id="$ask2", workspace=w.ws[SELF])
        self.assertTrue(first["matched"], first)
        again = policy.match_reply(w.tr[SELF], room=ROOM, self_mxid=SELF, event_id="$ask2", workspace=w.ws[SELF])
        self.assertEqual((again["matched"], again.get("replay")), (True, True))
        self.assertTrue(policy.mark_relayed(w.ws[SELF], C1)["ok"])
        done = policy.match_reply(w.tr[SELF], room=ROOM, self_mxid=SELF, event_id="$ask2", workspace=w.ws[SELF])
        self.assertIn("already answered", done["reason"])
        self.assertFalse(policy.mark_relayed(w.ws[SELF], C2)["ok"])


class TestOnwardInTheThread(unittest.TestCase):
    """B, running this skill, may consult C in the same thread; the thread is the chain. The
    loop check reads only who has posted in the thread and this agent's own asks."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)
        self.w.ask(SELF, SIB, cid=C1, task_id=TID, question="What is on the build host?")
        self.w.deliver(SIB, "$ask", "task-b1")

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_onward_ask_stays_in_the_thread_and_the_answer_flows_back(self):
        w = self.w
        onward = w.ask(SIB, THIRD, cid=C2, via_task="task-b1", question="Which jobs run on the build host?")
        self.assertEqual((onward["asked"], onward["root"]), (True, "$ask"), onward)
        self.assertEqual((w.tr[SIB].thread_roots, w.tr[SIB].reply_tos), (["$ask"], ["$ask"]))
        w.room.post(THIRD, "Nightly builds and the docs job.", thread_root="$ask")  # Muse-style reply
        w.deliver(SIB, "$ask3", "task-b2")
        up = w.match(SIB, "task-b2")
        self.assertEqual((up["matched"], up["answer_up"]["asker"]), (True, SELF), up)
        posted = w.answer(SIB, "Nightly builds and the docs job, per Third.", up=up["answer_up"]["up"])
        self.assertTrue(posted["answered"], posted)
        self.assertEqual((w.room.msgs[-1]["in_reply_to"], w.room.msgs[-1]["thread_root"]), ("$ask", "$ask"))
        w.deliver(SELF, posted["event_id"], "task-a2")
        top = w.match(SELF, "task-a2")
        self.assertEqual((top["matched"], top["task_id"]), (True, TID), top)
        self.assertEqual({m.get("thread_root") for m in w.room.msgs[1:]}, {"$ask"})

    def test_the_onward_ask_is_judged_not_final_by_the_first_asker(self):
        w = self.w
        w.ask(SIB, THIRD, cid=C2, via_task="task-b1", question="Which jobs run there?")
        waiting = policy.waiting_answers(w.tr[SELF], ROOM, w.ws[SELF])
        self.assertEqual(waiting[C1]["event_id"], "$ask2")  # B's onward ask is B's first message
        held = policy.match_reply(w.tr[SELF], room=ROOM, self_mxid=SELF, event_id="$ask2",
                                  workspace=w.ws[SELF], not_final=True)
        self.assertTrue(held["progress"])
        self.assertEqual(policy.waiting_answers(w.tr[SELF], ROOM, w.ws[SELF]), {})

    def test_no_agent_that_has_taken_part_is_asked_by_another(self):
        w = self.w
        w.ask(SIB, THIRD, cid=C2, via_task="task-b1", question="Which jobs?")
        w.room.post(THIRD, "Nightly builds.", thread_root="$ask")
        w.deliver(THIRD, "$ask2", "task-c1")
        for target in (SELF, SIB):
            res = w.ask(THIRD, target, cid=C3, via_task="task-c1")
            self.assertIn("already taken part", res["reason"], target)
        self.assertIn("already taken part", w.ask(SIB, SELF, cid=C3, via_task="task-b1")["reason"])
        self.assertIn("already taken part", w.ask(SELF, THIRD, cid=C3, task_id=TID)["reason"])
        self.assertTrue(w.ask(SELF, SIB, cid="4444444444444444", task_id=TID)["follow_up"])

    def test_the_loop_check_reads_senders_and_own_asks_only(self):
        msgs = [{"sender": SELF}, {"sender": SIB}]
        self.assertIsNone(policy.loop_check(msgs, [{"agent": SIB}], SIB))
        self.assertIsNone(policy.loop_check(msgs, [], THIRD))
        self.assertIsNotNone(policy.loop_check(msgs, [], SIB))


class TestIncomingAskGate(unittest.TestCase):
    """An incoming ask is answered or consulted from only if a verified task from the consult
    room delivered it, from another of the owner's agents, in a thread an owner's agent rooted,
    within this agent's window."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def refused(self, tid="task-b1"):
        res = self.w.ask(SIB, FOURTH, cid=C2, via_task=tid)
        self.assertFalse(res["asked"], res)
        self.assertEqual(self.w.tr[SIB].posted, [])
        return res["reason"]

    def test_the_genuine_ask_passes(self):
        self.w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.w.deliver(SIB, "$ask", "task-b1")
        self.assertTrue(self.w.ask(SIB, FOURTH, cid=C2, via_task="task-b1")["asked"])

    def test_an_unsigned_task_is_refused(self):
        self.w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.w.deliver(SIB, "$ask", "task-b1", stamp=False)
        self.assertIn("unsigned", self.refused())

    def test_a_task_from_another_room_is_refused(self):
        self.w.ask(SELF, SIB, cid=C1, task_id=TID)
        reply_task(self.w.ws[SIB], "$ask", tid="task-b1", room="!elsewhere:example.test", sender=SELF)
        self.assertIn("consult room", self.refused())

    def test_a_message_from_the_owner_human_is_not_an_agents_ask(self):
        self.w.room.post(OWNER, f"{SIB} — please check the host")
        self.w.deliver(SIB, "$ask", "task-b1")
        self.assertIn("not from another of the owner's agents", self.refused())

    def test_a_thread_rooted_by_the_owner_human_is_refused(self):
        self.w.room.post(OWNER, "a thread the owner started")
        self.w.room.post(SELF, f"{SIB} — please check", thread_root="$ask")
        self.w.deliver(SIB, "$ask2", "task-b1")
        self.assertIn("root is not from one of the owner's agents", self.refused())

    def test_a_closed_ask_task_is_refused(self):
        self.w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.w.deliver(SIB, "$ask", "task-b1")
        (self.w.ws[SIB] / "results" / "task-b1.txt").write_text("[no-send]")
        self.assertIn("already has a result", self.refused())


class TestPlainMessageEdges(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_refusals_on_the_ask_answer_and_match_paths(self):
        w = self.w
        self.assertIn("exactly one", w.ask(SELF, SIB, cid=C1, task_id=TID, via_task="task-b1")["reason"])
        self.assertIn("is not an onward consult", w.answer(SIB, "x", up=C2)["reason"])
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.room.post(SIB, "Yes.", thread_root="$ask")
        w.deliver(SELF, "$ask2", "task-a2", stamp=False)
        self.assertIn("unsigned", w.match(SELF, "task-a2")["reason"])
        reply_task(w.ws[SELF], "$ask2", tid="task-a3", room="!elsewhere:example.test", sender=SIB)
        self.assertIn("consult room", w.match(SELF, "task-a3")["reason"])
        self.assertEqual(policy.reply_lead({"channel_id": OWNER_DM, "thread_root": "$root"}),
                         f"[channel: {OWNER_DM}]\n[thread: $root]\n")

    def test_a_thread_out_of_sight_or_without_a_server_timestamp_refuses(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        w.room.post(OWNER, "later one")
        w.room.post(OWNER, "later two")
        with mock.patch.object(policy, "READ_LIMIT", 2):
            self.assertIn("cannot see the whole thread", w.ask(SELF, SIB, cid=C2, task_id=TID)["reason"])
        w.room.msgs[0].pop("ts")
        self.assertIn("no server timestamp", w.ask(SELF, SIB, cid=C2, task_id=TID)["reason"])
        w.deliver(SIB, "$ask", "task-b1")
        self.assertIn("no server timestamp", w.ask(SIB, THIRD, cid=C3, via_task="task-b1")["reason"])
        w.tr[SIB].read = lambda room, limit: {"ok": False, "reason": "down"}
        self.assertIn("unreadable", w.ask(SIB, THIRD, cid=C3, via_task="task-b1")["reason"])


class TestRecordEdges(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_record_stamped_in_the_future_expires(self):
        w = self.w
        policy.consult(w.tr[SELF], room=ROOM, self_mxid=SELF, agent=SIB, question="q", workspace=w.ws[SELF],
                       task_id=TID, cid=C1, now=lambda: T0 + 10 * 86400)
        [row] = policy.pending(w.ws[SELF], 600, now=T0)
        self.assertTrue(row["expired"])

    def test_an_expired_consult_is_pruned_once_the_owner_was_told(self):
        w = self.w
        policy.consult(w.tr[SELF], room=ROOM, self_mxid=SELF, agent=SIB, question="q", workspace=w.ws[SELF],
                       task_id=TID, cid=C1, now=lambda: T0)
        [row] = policy.pending(w.ws[SELF], 600, now=T0 + 7200, expire_after_s=3600)
        self.assertEqual((row["expired"], row["nudge_due"]), (True, True))
        policy.mark_nudged(w.ws[SELF], C1)
        self.assertEqual(len(policy.pending(w.ws[SELF], 600, now=T0 + 7200, expire_after_s=3600)), 1)
        (w.ws[SELF] / "results" / f"{TID}.txt").write_text("done")  # its task can no longer post
        self.assertEqual(policy.pending(w.ws[SELF], 600, now=T0 + 7200, expire_after_s=3600), [])
        self.assertEqual(pending_ids(w.ws[SELF]), [])

    def test_a_pending_copy_beside_an_answer_is_not_listed_for_a_nudge(self):
        w = self.w
        w.ask(SELF, SIB, cid=C1, task_id=TID)
        pend = w.ws[SELF] / policy._STATE / "pending" / f"{C1}.json"
        ans = w.ws[SELF] / policy._STATE / "answered" / f"{C1}.json"
        ans.parent.mkdir(parents=True, exist_ok=True)
        ans.write_text(pend.read_text())
        self.assertEqual(policy.pending(w.ws[SELF], 0), [])
        self.assertFalse(pend.exists())

    def test_a_match_landing_before_the_final_write_is_not_undone(self):
        w = self.w
        tr = w.tr[SELF]
        real = tr.mention

        def post_then_match_lands(*a, **k):
            out = real(*a, **k)
            pend = w.ws[SELF] / policy._STATE / "pending" / f"{C1}.json"
            ans = w.ws[SELF] / policy._STATE / "answered" / f"{C1}.json"
            ans.parent.mkdir(parents=True, exist_ok=True)
            os.link(pend, ans)
            pend.unlink()
            return out
        tr.mention = post_then_match_lands
        self.assertTrue(w.ask(SELF, SIB, cid=C1, task_id=TID)["asked"])
        self.assertEqual(pending_ids(w.ws[SELF]), [])

    def test_recovery_matches_only_this_agents_own_message_with_the_exact_text(self):
        rec = {"cid": C1, "agent": SIB, "ask_event": None, "root": None, "body": "is the disk full?"}
        forged = {"sender": THIRD, "event_id": "$x", "body": f"{SIB} — is the disk full?"}
        self.assertIsNone(policy._recover_ask(rec, [forged], SELF)["ask_event"])
        other_text = dict(forged, sender=SELF, body=f"{SIB} — is the disk full? (again)")
        self.assertIsNone(policy._recover_ask(rec, [other_text], SELF)["ask_event"])
        own = dict(forged, sender=SELF)
        self.assertEqual(policy._recover_ask(rec, [own], SELF)["ask_event"], "$x")
        self.assertIsNone(policy._recover_ask(rec, [own], SELF, used={"$x"})["ask_event"])

class TestPendingListing(unittest.TestCase):
    def test_overdue_consults_are_due_a_nudge_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp)
            run_consult(FakeTransport(), ws, now=1_000.0)
            [fresh] = policy.pending(ws, 600, now=1_100.0)
            self.assertEqual((fresh["overdue"], fresh["nudge_due"], fresh["age_s"]), (False, False, 100))
            [late] = policy.pending(ws, 600, now=2_000.0)
            self.assertEqual((late["overdue"], late["nudge_due"], late["task_id"]), (True, True, TID))
            self.assertTrue(policy.mark_nudged(ws, CID, now=2_001.0)["ok"])
            [told] = policy.pending(ws, 600, now=3_000.0)
            self.assertEqual((told["overdue"], told["nudge_due"]), (True, False))

    def test_answered_consults_are_not_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp)
            run_consult(FakeTransport(), ws)
            reply_task(ws, "$f")
            self.assertTrue(match(FakeTransport(replies=[answer_msg(in_reply_to="$ask")]), ws)["matched"])
            self.assertEqual(policy.pending(ws, 600), [])

    def test_mark_nudged_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("malformed", policy.mark_nudged(Path(tmp), "nope")["reason"])
            self.assertIn("no pending", policy.mark_nudged(Path(tmp), CID)["reason"])


class TestRelationMetadata(unittest.TestCase):
    def test_room_ops_read_keeps_relation_metadata(self):
        sys.path.insert(0, str(ROOM_OPS_CLI.parent))
        read = _load("room_ops_read", ROOM_OPS_CLI.parent / "read.py")
        out = read._normalize([
            {"event_id": "$a", "sender": SIB, "body": "x", "in_reply_to": "$ask"},
            {"event_id": "$b", "sender": SIB, "body": "y",
             "content": {"m.relates_to": {"rel_type": "m.thread", "event_id": "$root",
                                          "m.in_reply_to": {"event_id": "$ask"}}}},
            {"event_id": "$c", "sender": SIB, "body": "z"}])
        self.assertEqual(out[0]["in_reply_to"], "$ask")
        self.assertEqual((out[1]["in_reply_to"], out[1]["thread_root"]), ("$ask", "$root"))
        self.assertNotIn("in_reply_to", out[2])


class TestNoIdentityLiterals(unittest.TestCase):
    MXID = re.compile(r"@[A-Za-z0-9._=/+-]+:[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
    ROOM_ID = re.compile(r"![A-Za-z0-9._=-]+:[A-Za-z0-9-]+")
    HOST_PATH = re.compile(r"/(Users|home)/[A-Za-z0-9_.-]+")

    def test_patterns_detect_what_they_guard(self):
        sample = f"{SELF} {ROOM} /Users/someone/x"
        for rx in (self.MXID, self.ROOM_ID, self.HOST_PATH):
            self.assertTrue(rx.search(sample), rx.pattern)

    def test_skill_ships_no_mxid_room_id_or_host_path(self):
        found = []
        for p in SKILL.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                text = p.read_text(encoding="utf-8", errors="replace")
                for rx in (self.MXID, self.ROOM_ID, self.HOST_PATH):
                    found += [f"{p.relative_to(REPO)}: {m.group(0)}" for m in rx.finditer(text)]
        self.assertEqual(found, [])


class TestSkillBoundary(unittest.TestCase):
    """The transport is an injected CLI run through its public verbs; no other skill is imported."""

    def test_scripts_name_no_other_skill_and_import_none(self):
        for p in (SKILL / "scripts").glob("*.py"):
            text = p.read_text(encoding="utf-8")
            for other in ("agent-room-ops", "collaboration-intelligence"):
                self.assertNotIn(other, text, p.name)
            self.assertNotRegex(text, r"(?m)^\s*import (members|mention|read|resolve|lookup)\b", p.name)

    def test_transport_runs_the_configured_cli_verbs(self):
        calls = []

        def runner(argv, **kw):
            calls.append(argv[1:])
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")
        with tempfile.NamedTemporaryFile(suffix=".py") as f:
            tr = cli.RoomCliTransport(f.name, SELF, runner=runner)
            tr.agents(), tr.members(ROOM), tr.mention(SIB, "q", ROOM), tr.read(ROOM, 5)
            tr.mention(SIB, "q2", ROOM, reply_to="$ask", thread_root="$root")
            self.assertEqual(calls, [[f.name, "agents"], [f.name, "members", ROOM, "--agent", SELF],
                                     [f.name, "mention", SIB, "q", ROOM, "--agent", SELF],
                                     [f.name, "read", ROOM, "--limit", "5", "--agent", SELF],
                                     [f.name, "mention", SIB, "q2", ROOM, "--agent", SELF,
                                      "--reply-to", "$ask", "--thread-root", "$root"]])

    def test_transport_failures_are_in_band(self):
        with tempfile.NamedTemporaryFile(suffix=".py") as f:
            junk = cli.RoomCliTransport(f.name, SELF, runner=lambda argv, **kw:
                                        subprocess.CompletedProcess(argv, 1, stdout="Traceback", stderr=""))
            self.assertIn("no JSON", junk.agents()["reason"])

            def boom(argv, **kw):
                raise subprocess.TimeoutExpired(argv, 60)
            self.assertIn("failed", cli.RoomCliTransport(f.name, SELF, runner=boom).read(ROOM, 1)["reason"])
        with self.assertRaises(RuntimeError):
            cli.RoomCliTransport("relative/cli.py", SELF)


class FakeGateway(http.server.BaseHTTPRequestHandler):
    state = {}

    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/v1/agents":
            return self._send({"agents": self.state["agents"]})
        if path.endswith("/messages"):
            return self._send({"messages": self.state["messages"]})
        self._send({"error": "unknown"})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if req.get("op") == "members":
            return self._send({"ok": True, "members": self.state["members"]})
        if req.get("op") == "message":
            self.state.setdefault("posted", []).append(req)
            n = len(self.state["posted"])
            eid = "$ask" if n == 1 else f"$ask{n}"
            self.state["messages"].append({"event_id": eid, "sender": SELF, "ts": int(time.time() * 1000),
                                              "body": req["body"],
                                              **({"thread_root": req["thread_root"]}
                                                 if req.get("thread_root") else {})})
            return self._send({"ok": True, "event_id": eid})
        self._send({"error": "unknown op"})


class TestProductionAdapter(unittest.TestCase):
    """consult.py over the real room_ops CLI, against a local fake gateway."""

    def setUp(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        FakeGateway.state = {
            "agents": [{"id": SELF, "owner": OWNER}, {"id": SIB, "owner": OWNER}],
            "members": [{"user_id": SELF}, {"user_id": OWNER}, {"user_id": SIB}],
            "messages": [{"event_id": "$f", "sender": SIB, "ts": int(time.time() * 1000), "in_reply_to": "$ask",
                          "thread_root": "$ask", "body": f"{SELF} — {final(CID, 'host is up')}"},
                         {"event_id": "$p", "sender": SIB, "ts": int(time.time() * 1000), "in_reply_to": "$ask",
                          "thread_root": "$ask", "body": f"{SELF} — On it, checking now."}]}
        env = {"GATEWAY_URL": f"http://127.0.0.1:{self.srv.server_address[1]}",
               "GATEWAY_TOKEN": "test-token",
               "ROOM_OPS_GATE": str(Path(self.tmp.name) / "no-gate.json")}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.ws = make_ws(Path(self.tmp.name) / "ws")
        self.tr = cli.RoomCliTransport(str(ROOM_OPS_CLI), SELF)

    def tearDown(self):
        self.env.stop()
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def test_unidentifiable_member_refuses_through_the_real_cli(self):
        FakeGateway.state["members"].append({"display_name": "no id"})
        v = policy.guard(ROOM, SELF, self.tr)
        self.assertFalse(v["ok"])
        self.assertIn("1 member(s) with no identity", v["reason"])
        self.assertNotIn("posted", FakeGateway.state)

    def test_end_to_end_ask_then_the_reply_task_matches_after_a_judged_progress_message(self):
        res = run_consult(self.tr, self.ws)
        self.assertEqual((res["asked"], res["ask_event"]), (True, "$ask"), res)
        posted = FakeGateway.state["posted"]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["mentions"], [SIB])
        self.assertTrue(posted[0]["body"].endswith("is the build host up?"))
        self.assertNotIn("owner-agent-consult", posted[0]["body"])
        reply_task(self.ws, "$p", tid="task-progress")
        progress = policy.match_reply(self.tr, room=ROOM, self_mxid=SELF, task_id="task-progress",
                                      workspace=self.ws, not_final=True)  # the agent read it as progress
        self.assertTrue(progress.get("progress"), progress)
        reply_task(self.ws, "$f")
        got = match(self.tr, self.ws)
        self.assertTrue(got["matched"], got)
        self.assertEqual((got["task_id"], got["reply_text"]), (TID, "host is up"))

    def test_follow_up_posts_in_the_thread_through_the_real_cli(self):
        run_consult(self.tr, self.ws, cid=CID)
        res = run_consult(self.tr, self.ws, cid=CID_B, question="and the disk?")
        self.assertEqual((res["asked"], res["follow_up"]), (True, True), res)
        self.assertEqual(FakeGateway.state["posted"][1].get("thread_root"), "$ask")


class TestCliAndEdges(unittest.TestCase):
    def _cli(self, *argv, transport=None, env=None, workspace=None):
        out = io.StringIO()
        clean = {k: v for k, v in os.environ.items() if k not in CONFIG_KEYS}
        with mock.patch.dict(os.environ, {**clean, policy.CONFIG_ROOM: ROOM,
                                          policy.CONFIG_ROOM_CLI: "/x", **(env or {})},
                             clear=True), contextlib.redirect_stdout(out):
            rc = cli.main(list(argv), transport=transport,
                          workspace=workspace or Path(tempfile.gettempdir()))
        return rc, json.loads(out.getvalue())

    def test_cli_ask_match_and_pending_through_every_gate(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cli.policy, "new_cid", return_value=CID):
            ws = make_ws(Path(tmp) / "ws")
            q = Path(tmp) / "q.txt"
            q.write_text("is the build host up?", encoding="utf-8")
            tr = FakeTransport(replies=[answer_msg(text="up", in_reply_to="$ask")])
            rc, res = self._cli("ask", "--agent", SELF, "--agent-to", SIB, "--question-file", str(q),
                                "--task-id", TID, "--max-asks", "3", "--max-duration", "900",
                                transport=tr, workspace=ws)
            self.assertEqual((res["asked"], tr.reads, res["limits"]["max_asks"], res["limits"]["max_s"]),
                             (True, 0, 3, 900), res)
            rc, listed = self._cli("pending", "--nudge-after", "60", transport=tr, workspace=ws)
            self.assertEqual(([r["cid"] for r in listed["pending"]], listed["nudge_after_s"]), ([CID], 60))
            rc, nudged = self._cli("pending", "--nudged", CID, workspace=ws)
            self.assertTrue(nudged["ok"], nudged)
            reply_task(ws, "$f")
            rc, got = self._cli("match", "--agent", SELF, "--task-id", "task-reply",
                                transport=tr, workspace=ws)
            rc, again = self._cli("match", "--agent", SELF, "--event-id", "$f", transport=tr, workspace=ws)
            rc, rel = self._cli("relayed", CID, workspace=ws)
        self.assertTrue(got["matched"], got)
        self.assertEqual((got["task_id"], got["reply_text"]), (TID, "up"))
        self.assertIn("already answered", again["reason"])  # matched by a task: its result is the ack
        self.assertTrue(rel["ok"], rel)

    def test_cli_unreadable_question_and_refused_roster(self):
        rc, res = self._cli("ask", "--agent", SELF, "--agent-to", SIB, "--question-file",
                            "/nonexistent/q", "--task-id", TID, transport=FakeTransport())
        self.assertIn("unreadable ask text", res["reason"])
        tr = FakeTransport()
        tr._members.append({"user_id": STRANGER, "kind": "human"})
        rc, res = self._cli("roster", "--agent", SELF, transport=tr)
        self.assertFalse(res["ok"])
        self.assertIn(STRANGER, res["reason"])

    def test_missing_room_cli_is_named(self):
        rc, res = self._cli("roster", "--agent", SELF,
                            env={policy.CONFIG_ROOM_CLI: str(Path(tempfile.gettempdir()) / "absent.py")})
        self.assertIn(policy.CONFIG_ROOM_CLI, res["reason"])

    def test_workspace_resolves_through_its_owner(self):
        with mock.patch("workspace_default.resolve_workspace", return_value=Path("/ws")) as rw:
            self.assertEqual(cli._workspace(), Path("/ws"))
        rw.assert_called_once_with(migrate=False)

    def test_config_edges(self):
        self.assertEqual(policy.manifest_config(Path(tempfile.gettempdir()) / "absent.json"), {})
        self.assertEqual(policy.config_value(policy.CONFIG_ROOM, cli=" !r:x ", environ={}), "!r:x")
        s = policy.settings(environ={policy.CONFIG_ROOM: ROOM, policy.CONFIG_NUDGE_AFTER: "soon"},
                            manifest_cfg={})
        self.assertEqual(s["nudge_after_s"], policy.DEFAULT_NUDGE_AFTER_S)

    def test_guard_edges(self):
        tr = FakeTransport()
        tr.agents = lambda: {"ok": False, "reason": "503"}
        self.assertIn("registry unreadable", policy.guard(ROOM, SELF, tr)["reason"])
        tr = FakeTransport(members=[{"user_id": OWNER, "kind": "human"}])
        self.assertIn("not a member", policy.guard(ROOM, SELF, tr)["reason"])

    def test_consult_edges(self):
        self.assertIn("empty question", run_consult(FakeTransport(), question="  ")["reason"])
        self.assertIn("correlation id", run_consult(FakeTransport(), cid="nope")["reason"])
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp)
            run_consult(FakeTransport(), ws)
            reply_task(ws, "$f")
            tr = FakeTransport()
            tr.read = lambda room, limit: {"ok": False, "reason": "network error"}
            self.assertIn("unreadable: network error", match(tr, ws)["reason"])
            self.assertEqual(pending_ids(ws), [CID])


if __name__ == "__main__":
    unittest.main(verbosity=2)
