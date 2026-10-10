#!/usr/bin/env python3
"""skills/owner-agent-consult: inert until configured; the owner gate reads only a live,
envelope-verified, unanswered task by id; the owner-only room guard accounts for every
member; ask returns at once with a pending record; a whole consult, onward asks included,
runs in one thread; an onward ask must trace through the thread to a root ask; an agent
already in the chain is not asked again; a reply task matches its consult only by consult
id, sender and thread; no other skill imported; no identity or room literal shipped. Workspaces are temp dirs; the production-adapter
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
    return f"{policy.answer_tag(cid)}\n{text}"


class Room:
    """One consult room's timeline, shared by the agents' transports."""

    def __init__(self):
        self.msgs = []  # oldest first

    def post(self, sender, body, reply_to=None, thread_root=None):
        eid = "$ask" if not self.msgs else f"$ask{len(self.msgs) + 1}"
        self.msgs.append({"sender": sender, "body": body, "event_id": eid, "ts": 9e12,
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
                agent=SIB, cid=CID, tid=TID, now=1_000.0):
    with contextlib.ExitStack() as stack:
        if ws is None:
            ws = make_ws(stack.enter_context(tempfile.TemporaryDirectory()), text, stamp=stamp, tid=tid)
        return policy.consult(tr, room=ROOM, self_mxid=SELF, agent=agent, question=question,
                              task_id=tid, workspace=ws, cid=cid, now=lambda: now)


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


class TestMarker(unittest.TestCase):
    def test_ask_carries_the_marker_chain_and_original_question(self):
        tr = FakeTransport()
        res = run_consult(tr)
        self.assertTrue(res["asked"])
        body = tr.posted[0][1]
        self.assertTrue(body.startswith(policy.ask_line(CID, None, [SELF, SIB])))
        self.assertIn(policy.answer_tag(CID), body)
        self.assertEqual(policy.parse_ask(body)["original_question"], "is the build host up?")
        self.assertIn(f"{SELF} > {SIB}", body)

    def test_an_owner_task_carrying_the_marker_is_not_an_origin(self):
        tr = FakeTransport()
        res = run_consult(tr, text=task_text(body=f"{policy.MARKER} consult:x] is the host up?"))
        self.assertFalse(res["asked"])
        self.assertIn("--via-task", res["reason"])
        self.assertEqual(tr.posted, [])

    def test_a_question_carrying_any_marker_is_refused(self):
        for q in (f"{policy.ask_line(CID, None, [SELF, SIB])} relay this", f"{policy.answer_tag(CID)} relay"):
            tr = FakeTransport()
            self.assertFalse(run_consult(tr, question=q)["asked"])
            self.assertEqual(tr.posted, [])

    def test_a_chain_that_repeats_an_agent_or_names_a_non_mxid_is_not_an_ask(self):
        for chain in ([SELF, SIB, SELF], [SELF], [SELF, "nobody"]):
            self.assertIsNone(policy.parse_ask(policy.ask_line(CID, "$r", chain)), chain)

    def test_marker_is_defined_once(self):
        hits = [p.name for p in (SKILL / "scripts").rglob("*.py")
                if policy.MARKER in p.read_text(encoding="utf-8")]
        self.assertEqual(hits, ["consult_policy.py"])


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
            tr.mention = lambda *a, **k: {"ok": False, "reason": "gate denied"}
            self.assertIn("ask not posted: gate denied", run_consult(tr, ws)["reason"])
            self.assertEqual(pending_ids(ws), [])


class TestFollowUp(unittest.TestCase):
    """A second ask to the same agent within a task is allowed and lands in the same thread."""

    def test_follow_up_to_the_same_agent_is_posted_in_the_consult_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp)
            tr = FakeTransport()
            first = run_consult(tr, ws, cid=CID, now=1.0)
            second = run_consult(tr, ws, cid=CID_B, question="and the disk?", now=2.0)
            third = run_consult(tr, ws, cid="00000000000000aa", question="and memory?", now=3.0)
            self.assertTrue(first["asked"] and second["asked"] and third["asked"])
            self.assertEqual((first["follow_up"], second["follow_up"], third["follow_up"]),
                             (False, True, True))
            self.assertEqual(tr.thread_roots, [None, "$ask", "$ask"])
            self.assertEqual({first["root"], second["root"], third["root"]}, {"$ask"})
            self.assertEqual(sorted(pending_ids(ws)), sorted([CID, CID_B, "00000000000000aa"]))
            self.assertIn("is the build host up?", policy.parse_ask(tr.posted[1][1])["original_question"])

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


class TestMatchReply(unittest.TestCase):
    """A reply task binds to its consult only by consult id, sender and relation to the ask."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_ws(self._tmp.name)
        run_consult(FakeTransport(), self.ws)

    def tearDown(self):
        self._tmp.cleanup()

    def _match(self, *msgs, event="$f", **kw):
        reply_task(self.ws, event, **kw)
        return match(FakeTransport(replies=list(msgs)), self.ws)

    def test_the_correlated_answer_returns_the_original_task_and_closes_it(self):
        res = self._match(answer_msg(in_reply_to="$ask"))
        self.assertTrue(res["matched"], res)
        self.assertEqual((res["task_id"], res["agent"], res["cid"], res["reply_text"]),
                         (TID, SIB, CID, "host is up"))
        self.assertEqual(res["lead"], f"[channel: {OWNER_DM}]\n")
        self.assertEqual(pending_ids(self.ws), [])
        again = match(FakeTransport(replies=[answer_msg(in_reply_to="$ask")]), self.ws)
        self.assertFalse(again["matched"])
        self.assertIn("already answered", again["reason"])

    def test_a_threaded_original_gets_a_thread_lead(self):
        self.assertEqual(policy.reply_lead({"channel_id": OWNER_DM, "thread_root": "$root"}),
                         f"[channel: {OWNER_DM}]\n[thread: $root]\n")

    def test_wrong_consult_id_does_not_match(self):
        res = self._match(answer_msg(cid=CID_B, in_reply_to="$ask"))
        self.assertFalse(res["matched"])
        self.assertIn(CID_B, res["reason"])
        self.assertEqual(pending_ids(self.ws), [CID])

    def test_not_a_reply_to_the_ask_does_not_match(self):
        res = self._match(answer_msg(in_reply_to="$someone-else"))
        self.assertFalse(res["matched"])
        self.assertIn("not this consult's ask", res["reason"])
        self.assertEqual(pending_ids(self.ws), [CID])

    def test_an_answer_outside_the_consult_thread_does_not_match(self):
        for root in ("$other-thread", None):
            res = self._match(answer_msg(thread_root=root))
            self.assertFalse(res["matched"], root)
            self.assertIn("not posted in this consult's thread", res["reason"])
        self.assertEqual(pending_ids(self.ws), [CID])

    def test_a_progress_message_is_progress(self):
        res = self._match({"sender": SIB, "body": f"{SELF} — On it, checking now.", "event_id": "$f",
                           "ts": 9e12, "in_reply_to": "$ask"})
        self.assertEqual((res["matched"], res.get("progress")), (False, True))
        self.assertEqual(pending_ids(self.ws), [CID])

    def test_an_answer_from_another_sender_does_not_match(self):
        res = self._match(answer_msg(sender=OWNER, in_reply_to="$ask"))
        self.assertFalse(res["matched"])
        self.assertIn("not " + SIB, res["reason"])

    def test_the_reply_task_must_be_verified_and_from_the_consult_room(self):
        self.assertIn("unsigned", self._match(answer_msg(), stamp=False)["reason"])
        self.assertIn("consult room", self._match(answer_msg(), room="!elsewhere:example.test")["reason"])
        self.assertIn("not a live task", match(FakeTransport(), self.ws, tid="task-absent")["reason"])
        self.assertEqual(pending_ids(self.ws), [CID])

    def test_an_event_outside_the_read_window_says_so(self):
        res = self._match(answer_msg(event="$elsewhere"))
        self.assertIn(f"last {policy.READ_LIMIT} messages", res["reason"])

    def test_an_answer_line_with_no_answer_is_refused(self):
        msg = {"sender": SIB, "body": policy.answer_tag(CID), "event_id": "$f", "ts": 9e12,
               "thread_root": "$ask"}
        self.assertIn("no answer", self._match(msg)["reason"])

    def test_a_follow_up_answer_in_the_thread_matches(self):
        tr = FakeTransport()
        tr.room.post(SELF, "first ask")  # stands in for the $ask already posted
        with mock.patch.object(policy, "thread_view", return_value={"ok": True, "msgs": []}):
            res = run_consult(tr, self.ws, cid=CID_B)
        self.assertEqual((res["asked"], res["ask_event"], res["root"]), (True, "$ask2", "$ask"), res)
        got = self._match(answer_msg(cid=CID_B, in_reply_to="$ask2"))
        self.assertTrue(got["matched"], got)
        self.assertEqual(pending_ids(self.ws), [CID])

    def test_an_ask_delivered_as_a_reply_is_not_an_answer(self):
        msg = {"sender": SIB, "body": f"{SELF} — {policy.ask_line(CID_B, '$ask', [SIB, SELF])}",
               "event_id": "$f", "thread_root": "$ask"}
        res = self._match(msg)
        self.assertFalse(res["matched"])
        self.assertIn("consult ask to you", res["reason"])


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

    def deliver(self, to, event, tid, *, stamp=True):
        sender = next(m["sender"] for m in self.room.msgs if m["event_id"] == event)
        return reply_task(self.ws[to], event, tid=tid, sender=sender, stamp=stamp)

    def ask(self, me, agent, *, cid, task_id=None, via_task=None, question="is the disk full?"):
        return policy.consult(self.tr[me], room=ROOM, self_mxid=me, agent=agent, question=question,
                              workspace=self.ws[me], task_id=task_id, via_task=via_task, cid=cid,
                              now=lambda: 1.0)

    def answer(self, me, text, *, task_id=None, up=None):
        return policy.answer(self.tr[me], room=ROOM, self_mxid=me, text=text, workspace=self.ws[me],
                             task_id=task_id, up=up)

    def match(self, me, tid):
        return policy.match_reply(self.tr[me], room=ROOM, self_mxid=me, task_id=tid, workspace=self.ws[me])


C1, C2, C3 = "1111111111111111", "2222222222222222", "3333333333333333"


class TestOnwardConsult(unittest.TestCase):
    """A consulted agent may consult another of the owner's agents; the whole consult runs in
    the thread rooted on the first ask, and answers flow back up the chain to the owner."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)
        self.root = self.w.ask(SELF, SIB, cid=C1, task_id=TID, question="what is on the build host?")
        self.w.deliver(SIB, "$ask", "task-b1")

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_onward_consult_is_allowed_and_names_the_original_question_and_chain(self):
        res = self.w.ask(SIB, THIRD, cid=C2, via_task="task-b1")
        self.assertEqual((res["asked"], res["root"], res["chain"]), (True, "$ask", [SELF, SIB, THIRD]), res)
        body = self.w.tr[SIB].posted[0][1]
        parsed = policy.parse_ask(body)
        self.assertEqual((parsed["root"], parsed["chain"]), ("$ask", [SELF, SIB, THIRD]))
        self.assertEqual(parsed["original_question"], "what is on the build host?")
        self.assertIn(f"{SELF} > {SIB} > {THIRD}", body)
        [rec] = policy.records(self.w.ws[SIB], "pending")
        self.assertEqual((rec["root"], rec["chain"], rec["via"]["asker"], rec["via"]["cid"], rec["task_id"]),
                         ("$ask", [SELF, SIB, THIRD], SELF, C1, None))

    def test_every_post_of_a_chained_consult_is_in_the_one_thread(self):
        w = self.w
        self.assertTrue(w.ask(SIB, THIRD, cid=C2, via_task="task-b1")["asked"])
        w.deliver(THIRD, "$ask2", "task-c1")
        self.assertTrue(w.answer(THIRD, "disk is 80% full", task_id="task-c1")["answered"])
        w.deliver(SIB, "$ask3", "task-b2")
        up = w.match(SIB, "task-b2")
        self.assertTrue(up["matched"], up)
        self.assertEqual((up["task_id"], up["answer_up"]["asker"], up["answer_up"]["cid"], up["reply_text"]),
                         (None, SELF, C1, "disk is 80% full"))
        self.assertTrue(w.answer(SIB, "per third: disk is 80% full", up=up["answer_up"]["up"])["answered"])
        w.deliver(SELF, "$ask4", "task-a2")
        top = w.match(SELF, "task-a2")
        self.assertTrue(top["matched"], top)
        self.assertEqual((top["task_id"], top["reply_text"], top["lead"]),
                         (TID, "per third: disk is 80% full", f"[channel: {OWNER_DM}]\n"))
        root, *rest = w.room.msgs
        self.assertNotIn("thread_root", root)
        self.assertEqual([m["thread_root"] for m in rest], ["$ask"] * 3)
        self.assertEqual([m["sender"] for m in w.room.msgs], [SELF, SIB, THIRD, SIB])

    def test_an_agent_already_in_the_chain_is_not_asked_again(self):
        w = self.w
        w.ask(SIB, THIRD, cid=C2, via_task="task-b1")
        w.deliver(THIRD, "$ask2", "task-c1")
        for upstream in (SELF, SIB):
            res = w.ask(THIRD, upstream, cid=C3, via_task="task-c1")
            self.assertFalse(res["asked"], upstream)
            self.assertIn("already in this consult's chain", res["reason"])
        self.assertEqual(w.tr[THIRD].posted, [])
        res = w.ask(SELF, THIRD, cid=C3, task_id=TID)
        self.assertFalse(res["asked"])
        self.assertIn("answer from what the thread has", res["reason"])
        again = w.ask(SELF, SIB, cid="4444444444444444", task_id=TID)
        self.assertEqual((again["asked"], again["follow_up"], again["root"]), (True, True, "$ask"), again)

    def test_the_loop_check_reads_the_chain_not_the_text(self):
        asks = [{"chain": [SELF, SIB]}, {"chain": [SELF, SIB, THIRD]}]
        self.assertIsNone(policy.loop_check(asks, SELF, SIB))
        self.assertIsNone(policy.loop_check(asks, SIB, THIRD))
        self.assertIsNone(policy.loop_check(asks, SIB, FOURTH))
        self.assertIsNotNone(policy.loop_check(asks, THIRD, SELF))
        self.assertIsNotNone(policy.loop_check(asks, SELF, THIRD))


class TestOnwardTracesToAnOwnerTask(unittest.TestCase):
    """An onward ask must come from a verified task delivering an ask whose chain the thread
    shows link by link back to a root ask, which only the verified-owner-task gate posts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def onward(self, tid="task-b1"):
        res = self.w.ask(SIB, FOURTH, cid=C2, via_task=tid)
        self.assertFalse(res["asked"], res)
        self.assertEqual(self.w.tr[SIB].posted, [])
        return res["reason"]

    def test_the_genuine_chain_passes(self):
        self.w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.w.deliver(SIB, "$ask", "task-b1")
        self.assertTrue(self.w.ask(SIB, FOURTH, cid=C2, via_task="task-b1")["asked"])

    def test_no_owner_task_and_no_ask_is_refused(self):
        res = self.w.ask(SIB, FOURTH, cid=C2)
        self.assertIn("exactly one", res["reason"])

    def test_an_unsigned_via_task_is_refused(self):
        self.w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.w.deliver(SIB, "$ask", "task-b1", stamp=False)
        self.assertIn("unsigned", self.onward())

    def test_a_via_task_that_is_not_an_ask_is_refused(self):
        self.w.room.post(SELF, f"{SIB} — please look at the host")
        self.w.deliver(SIB, "$ask", "task-b1")
        self.assertIn("not a consult ask", self.onward())

    def test_an_ask_sent_by_someone_other_than_its_asker_is_refused(self):
        self.w.room.post(THIRD, f"{SIB} — {policy.ask_line(C1, None, [SELF, SIB])}")
        self.w.deliver(SIB, "$ask", "task-b1")
        self.assertIn("not addressed to this agent", self.onward())

    def test_a_chain_with_a_hop_the_thread_does_not_show_is_refused(self):
        self.w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.w.room.post(THIRD, f"{SIB} — {policy.ask_line(C3, '$ask', [SELF, THIRD, SIB])}",
                         thread_root="$ask")
        self.w.deliver(SIB, "$ask2", "task-b1")
        self.assertIn("does not trace to the root ask", self.onward())

    def test_a_root_that_is_not_a_first_ask_is_refused(self):
        self.w.room.post(SELF, "hello")
        self.w.room.post(SELF, f"{SIB} — {policy.ask_line(C1, '$ask', [SELF, SIB])}", thread_root="$ask")
        self.w.deliver(SIB, "$ask2", "task-b1")
        self.assertIn("thread root is not a consult ask", self.onward())

    def test_a_first_ask_posted_inside_a_thread_is_refused(self):
        self.w.room.post(SELF, "hello")
        self.w.room.post(SELF, f"{SIB} — {policy.ask_line(C1, None, [SELF, SIB])}", thread_root="$ask")
        self.w.deliver(SIB, "$ask2", "task-b1")
        self.assertIn("first ask must be a direct ask", self.onward())

    def test_an_ask_outside_its_thread_is_refused(self):
        self.w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.w.room.post(SELF, f"{SIB} — {policy.ask_line(C3, '$ask', [SELF, SIB])}")
        self.w.deliver(SIB, "$ask2", "task-b1")
        self.assertIn("not posted in its consult thread", self.onward())

    def test_a_root_out_of_sight_is_refused(self):
        self.w.ask(SELF, SIB, cid=C1, task_id=TID)
        self.w.ask(SELF, THIRD, cid=C3, task_id=TID)
        self.w.deliver(THIRD, "$ask2", "task-c1")
        with mock.patch.object(policy, "READ_LIMIT", 1):
            res = self.w.ask(THIRD, FOURTH, cid=C2, via_task="task-c1")
        self.assertIn("cannot see the whole thread", res["reason"])

    def test_answer_also_requires_a_traceable_ask(self):
        self.w.room.post(THIRD, f"{SIB} — {policy.ask_line(C1, None, [SELF, SIB])}")
        self.w.deliver(SIB, "$ask", "task-b1")
        res = self.w.answer(SIB, "x", task_id="task-b1")
        self.assertFalse(res["answered"])
        self.assertEqual(self.w.tr[SIB].posted, [])
        self.assertIn("is not an onward consult", self.w.answer(SIB, "x", up=C2)["reason"])


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
        self.assertIn("carries the consult marker", w.answer(SIB, policy.answer_tag(C1), task_id="task-b1")["reason"])
        w.tr[SIB]._members.append({"user_id": STRANGER, "kind": "human"})
        self.assertIn(STRANGER, w.answer(SIB, "x", task_id="task-b1")["reason"])
        w.tr[SIB]._members.pop()
        w.tr[SIB].mention = lambda *a, **k: {"ok": False, "reason": "gate denied"}
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
        rec_path = ws / policy._STATE / "pending" / f"{C1}.json"
        rec = json.loads(rec_path.read_text())
        rec_path.write_text(json.dumps({**rec, "ask_event": None}))
        self.assertIn("never confirmed", w.match(SELF, "task-a2")["reason"])
        rec_path.write_text(json.dumps(rec))
        with mock.patch.object(policy.os, "rename", side_effect=FileNotFoundError):
            self.assertIn("already answered", w.match(SELF, "task-a2")["reason"])
        with mock.patch.object(policy.os, "rename", side_effect=PermissionError("ro")):
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
        self.assertIsNone(policy.parse_ask(policy.ask_line(C1, "not-an-event", [SELF, SIB])))


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
            self.state["messages"].insert(0, {"event_id": eid, "sender": SELF, "ts": 7e12, "body": req["body"],
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
            "messages": [{"event_id": "$f", "sender": SIB, "ts": 9e12, "in_reply_to": "$ask",
                          "thread_root": "$ask", "body": f"{SELF} — {final(CID, 'host is up')}"},
                         {"event_id": "$p", "sender": SIB, "ts": 8e12, "in_reply_to": "$ask",
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

    def test_end_to_end_ask_then_the_reply_task_matches_and_the_checkpoint_does_not(self):
        res = run_consult(self.tr, self.ws)
        self.assertEqual((res["asked"], res["ask_event"]), (True, "$ask"), res)
        posted = FakeGateway.state["posted"]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["mentions"], [SIB])
        self.assertIn(f"consult:{CID}", posted[0]["body"])
        reply_task(self.ws, "$p", tid="task-progress")
        self.assertTrue(match(self.tr, self.ws, "task-progress").get("progress"))
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
                                "--task-id", TID, transport=tr, workspace=ws)
            self.assertEqual((res["asked"], tr.reads), (True, 0), res)
            rc, listed = self._cli("pending", "--nudge-after", "60", workspace=ws)
            self.assertEqual(([r["cid"] for r in listed["pending"]], listed["nudge_after_s"]), ([CID], 60))
            rc, nudged = self._cli("pending", "--nudged", CID, workspace=ws)
            self.assertTrue(nudged["ok"], nudged)
            reply_task(ws, "$f")
            rc, got = self._cli("match", "--agent", SELF, "--task-id", "task-reply",
                                transport=tr, workspace=ws)
            rc, ans = self._cli("answer", "--agent", SELF, "--task-id", "task-reply", "--body-file", str(q),
                                transport=tr, workspace=ws)
        self.assertTrue(got["matched"], got)
        self.assertEqual((ans["answered"], ans["reason"]), (False, "task is not a consult ask"))
        self.assertEqual((got["task_id"], got["reply_text"]), (TID, "up"))

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
