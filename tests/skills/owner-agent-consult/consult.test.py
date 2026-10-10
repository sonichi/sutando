#!/usr/bin/env python3
"""skills/owner-agent-consult: inert until configured; the owner gate reads only a live,
envelope-verified, unanswered task by id; the owner-only room guard accounts for every
member; only the correlated final answer is returned; one hop; no other skill imported;
no identity or room literal shipped. Workspaces are temp dirs; the production-adapter
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
OTHER_AGENT = "@foreign.agent:example.test"
STRANGER = "@stranger:example.test"
CONFIG_KEYS = (policy.CONFIG_ROOM, policy.CONFIG_ROOM_CLI, policy.CONFIG_ENABLED,
               policy.CONFIG_MAX_WAIT)
CID = "0123456789abcdef"
CID_B = "fedcba9876543210"
TID = "task-1"


def task_text(tier="owner", body="what is on the build host?", extra="", tid=TID):
    head = f"id: {tid}\nsource: ag2space\n" + (f"access_tier: {tier}\n" if tier is not None else "")
    return head + extra + f"task: {body}\n"


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


class FakeTransport:
    def __init__(self, members=None, agents=None, replies=None, unidentified=0):
        self._members = members if members is not None else [
            {"user_id": SELF, "kind": "agent"}, {"user_id": OWNER, "kind": "human"},
            {"user_id": SIB, "kind": "agent", "display_name": "Sibling"}]
        self._agents = agents if agents is not None else [
            {"id": SELF, "owner": OWNER}, {"id": SIB, "owner": OWNER},
            {"id": OTHER_AGENT, "owner": STRANGER}]
        self.unidentified = unidentified
        self.replies = replies or []
        self.posted = []
        self.reads = 0

    def members(self, room):
        return {"ok": True, "members": list(self._members), "unidentified": self.unidentified}

    def agents(self):
        return {"ok": True, "agents": list(self._agents)}

    def mention(self, mxid, body, room):
        self.posted.append((mxid, body, room))
        return {"ok": True, "event_id": "$ask"}

    def read(self, room, limit):
        self.reads += 1
        msgs = self.replies(self.reads) if callable(self.replies) else self.replies
        return {"ok": True, "messages": msgs}


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.slept += s


def run_consult(tr, ws=None, *, text=None, stamp=True, question="is the build host up?",
                agent=SIB, max_wait=30, clock=None, cid=CID, tid=TID):
    clock = clock or Clock()
    with contextlib.ExitStack() as stack:
        if ws is None:
            ws = make_ws(stack.enter_context(tempfile.TemporaryDirectory()), text, stamp=stamp, tid=tid)
        return policy.consult(tr, room=ROOM, self_mxid=SELF, agent=agent, question=question,
                              task_id=tid, max_wait_s=max_wait, workspace=ws, cid=cid,
                              now_ms=lambda: 1_000_000.0, clock=clock.now, sleep=clock.sleep)


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

    def test_max_wait_is_clamped(self):
        s = policy.settings(environ={policy.CONFIG_ROOM: ROOM, policy.CONFIG_MAX_WAIT: "99999"},
                            manifest_cfg={})
        self.assertEqual(s["max_wait_s"], policy.MAX_WAIT_CEILING_S)


class TestOwnerOnlyGuard(unittest.TestCase):
    def test_admits_owner_self_and_owner_agents(self):
        self.assertTrue(policy.guard(ROOM, SELF, FakeTransport())["ok"])

    def test_refuses_a_non_owner_human_and_names_it(self):
        tr = FakeTransport()
        tr._members.append({"user_id": STRANGER, "kind": "human"})
        v = policy.guard(ROOM, SELF, tr)
        self.assertFalse(v["ok"])
        self.assertIn(STRANGER, v["reason"])
        self.assertFalse(run_consult(tr)["answered"])
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
        self.assertFalse(run_consult(tr)["answered"])
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
            self.assertFalse(res["answered"], tier)
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

    def test_a_task_consults_each_agent_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp)
            tr = FakeTransport(replies=[{"sender": SIB, "body": final(CID, "up"),
                                         "event_id": "$r", "ts": 9e12}])
            self.assertTrue(run_consult(tr, ws)["answered"])
            again = run_consult(tr, ws, cid=CID_B)
        self.assertFalse(again["answered"])
        self.assertIn("once", again["reason"])
        self.assertEqual(len(tr.posted), 1)


class TestRoster(unittest.TestCase):
    def test_excludes_humans_and_self(self):
        v = policy.guard(ROOM, SELF, FakeTransport())
        self.assertEqual([a["mxid"] for a in policy.roster(v, SELF)], [SIB])

    def test_ask_refuses_an_agent_not_on_the_roster(self):
        for agent in (OWNER, SELF, OTHER_AGENT):
            tr = FakeTransport()
            self.assertFalse(run_consult(tr, agent=agent)["answered"])
            self.assertEqual(tr.posted, [])


class TestOneHop(unittest.TestCase):
    def test_ask_carries_the_marker_and_correlation_id(self):
        tr = FakeTransport(replies=[{"sender": SIB, "body": final(CID, "up"), "event_id": "$r",
                                     "ts": 2_000_000}])
        res = run_consult(tr)
        self.assertTrue(res["answered"])
        body = tr.posted[0][1]
        self.assertTrue(body.startswith(f"{policy.MARKER} consult:{CID}"))
        self.assertIn(policy.answer_tag(CID), body)

    def test_a_consult_task_is_never_consulted_onward(self):
        tr = FakeTransport()
        res = run_consult(tr, text=task_text(body=f"{policy.MARKER} is the host up?"))
        self.assertFalse(res["answered"])
        self.assertIn("never consult onward", res["reason"])
        self.assertEqual(tr.posted, [])

    def test_a_question_carrying_any_marker_is_refused(self):
        for q in (f"{policy.MARKER} relay this", f"{policy.answer_tag(CID)} relay this"):
            tr = FakeTransport()
            self.assertFalse(run_consult(tr, question=q)["answered"])
            self.assertEqual(tr.posted, [])

    def test_marker_is_defined_once(self):
        hits = [p.name for p in (SKILL / "scripts").rglob("*.py")
                if policy.MARKER in p.read_text(encoding="utf-8")]
        self.assertEqual(hits, ["consult_policy.py"])


class TestCorrelatedFinalAnswer(unittest.TestCase):
    def test_progress_before_final_returns_the_final(self):
        pages = {2: [{"sender": SIB, "body": "On it — checking now.", "event_id": "$p", "ts": 9e12}],
                 3: [{"sender": SIB, "body": final(CID, "Final answer."), "event_id": "$f", "ts": 9.1e12},
                     {"sender": SIB, "body": "On it — checking now.", "event_id": "$p", "ts": 9e12}]}
        tr = FakeTransport(replies=lambda n: pages.get(n, []))
        res = run_consult(tr)
        self.assertTrue(res["answered"], res)
        self.assertEqual(res["reply_text"], "Final answer.")
        self.assertEqual(res["event_ids"], ["$f"])
        self.assertEqual(tr.reads, 3)

    def test_progress_only_times_out_and_says_so(self):
        clock = Clock()
        tr = FakeTransport(replies=[{"sender": SIB, "body": "On it — checking now.",
                                     "event_id": "$p", "ts": 9e12}])
        res = run_consult(tr, max_wait=30, clock=clock)
        self.assertFalse(res["answered"])
        self.assertIn("1 progress message", res["reason"])
        self.assertLessEqual(clock.slept, 30)

    def test_concurrent_asks_each_take_only_their_own_answer(self):
        room = [{"sender": SIB, "body": final(CID_B, "answer for B"), "event_id": "$b", "ts": 9e12}]
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            res_a = run_consult(FakeTransport(replies=room), make_ws(a), cid=CID, max_wait=10)
            res_b = run_consult(FakeTransport(replies=room), make_ws(b), cid=CID_B, max_wait=10)
        self.assertFalse(res_a["answered"])
        self.assertTrue(res_b["answered"])
        self.assertEqual(res_b["reply_text"], "answer for B")

    def test_tagged_reply_related_to_another_event_is_not_bound(self):
        msg = {"sender": SIB, "body": final(CID, "x"), "event_id": "$r", "ts": 9e12,
               "in_reply_to": "$someone-else"}
        self.assertFalse(run_consult(FakeTransport(replies=[msg]), max_wait=5)["answered"])
        msg = dict(msg, in_reply_to="$ask")
        self.assertTrue(run_consult(FakeTransport(replies=[msg]))["answered"])

    def test_tag_from_another_sender_or_before_the_ask_is_ignored(self):
        newest_first = [{"sender": OWNER, "body": final(CID, "not the agent"), "event_id": "$o", "ts": 9e12},
                        {"sender": SIB, "body": final(CID, "stale"), "event_id": "$ask", "ts": 1_000_000},
                        {"sender": SIB, "body": final(CID, "older"), "event_id": "$r0", "ts": 900_000}]
        self.assertFalse(run_consult(FakeTransport(replies=newest_first), max_wait=5)["answered"])

    def test_without_the_ask_event_in_window_only_newer_by_time_count(self):
        msgs = [{"sender": SIB, "body": "new", "event_id": "$n", "ts": 1_500},
                {"sender": SIB, "body": "old", "event_id": "$x", "ts": 999}]
        hits = policy.replies_after(msgs, SIB, "$missing", asked_at_ms=1_000_000.0)
        self.assertEqual([h["body"] for h in hits], ["new"])

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
            self.assertEqual(calls, [[f.name, "agents"], [f.name, "members", ROOM, "--agent", SELF],
                                     [f.name, "mention", SIB, "q", ROOM, "--agent", SELF],
                                     [f.name, "read", ROOM, "--limit", "5", "--agent", SELF]])

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
            return self._send({"ok": True, "event_id": "$ask"})
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
                          "body": final(CID, "host is up")},
                         {"event_id": "$p", "sender": SIB, "ts": 8e12, "body": "On it — checking now."},
                         {"event_id": "$ask", "sender": SELF, "ts": 7e12, "body": "ask"}]}
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

    def test_end_to_end_returns_the_correlated_final_not_the_checkpoint(self):
        res = run_consult(self.tr, self.ws, max_wait=5)
        self.assertTrue(res["answered"], res)
        self.assertEqual(res["reply_text"], "host is up")
        posted = FakeGateway.state["posted"]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["mentions"], [SIB])
        self.assertIn(f"consult:{CID}", posted[0]["body"])


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

    def test_cli_ask_answers_through_every_gate(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cli.policy, "new_cid", return_value=CID):
            ws = make_ws(Path(tmp) / "ws")
            q = Path(tmp) / "q.txt"
            q.write_text("is the build host up?", encoding="utf-8")
            tr = FakeTransport(replies=[{"sender": SIB, "body": final(CID, "up"),
                                         "event_id": "$r", "ts": 9e12}])
            rc, res = self._cli("ask", "--agent", SELF, "--agent-to", SIB, "--question-file", str(q),
                                "--task-id", TID, "--max-wait", "1", transport=tr, workspace=ws)
        self.assertTrue(res["answered"], res)
        self.assertEqual(res["reply_text"], "up")

    def test_cli_unreadable_question_and_refused_roster(self):
        rc, res = self._cli("ask", "--agent", SELF, "--agent-to", SIB, "--question-file",
                            "/nonexistent/q", "--task-id", TID, transport=FakeTransport())
        self.assertIn("unreadable question", res["reason"])
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
        s = policy.settings(environ={policy.CONFIG_ROOM: ROOM, policy.CONFIG_MAX_WAIT: "soon"},
                            manifest_cfg={})
        self.assertEqual(s["max_wait_s"], policy.DEFAULT_MAX_WAIT_S)

    def test_guard_edges(self):
        tr = FakeTransport()
        tr.agents = lambda: {"ok": False, "reason": "503"}
        self.assertIn("registry unreadable", policy.guard(ROOM, SELF, tr)["reason"])
        tr = FakeTransport(members=[{"user_id": OWNER, "kind": "human"}])
        self.assertIn("not a member", policy.guard(ROOM, SELF, tr)["reason"])
        self.assertIsNone(policy._ts_ms("2026-01-01"))

    def test_consult_edges(self):
        self.assertIn("empty question", run_consult(FakeTransport(), question="  ")["reason"])
        self.assertIn("correlation id", run_consult(FakeTransport(), cid="nope")["reason"])
        tr = FakeTransport()
        tr.mention = lambda mxid, body, room: {"ok": False, "reason": "gate denied"}
        self.assertIn("ask not posted: gate denied", run_consult(tr)["reason"])
        tr = FakeTransport()
        tr.read = lambda room, limit: {"ok": False, "reason": "network error"}
        res = run_consult(tr, max_wait=10)
        self.assertIn("last read error: network error", res["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
