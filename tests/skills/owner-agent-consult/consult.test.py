#!/usr/bin/env python3
"""skills/owner-agent-consult: inert until configured, owner-only room guard, owner-tier
gate through the attested header path, roster from registry + room membership, one-hop
marker, bounded wait, and no identity or room literal shipped in the skill.
The room transport is a stub; no gateway, room or workspace is touched."""
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SKILL = REPO / "skills" / "owner-agent-consult"
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(REPO / "src"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


policy = _load("consult_policy", SKILL / "scripts" / "consult_policy.py")
cli = _load("consult_cli", SKILL / "scripts" / "consult.py")

ROOM = "!consult:example.test"
SELF = "@self.agent:example.test"
OWNER = "@owner:example.test"
SIB = "@sibling.agent:example.test"
OTHER_AGENT = "@foreign.agent:example.test"
STRANGER = "@stranger:example.test"
CONFIG_KEYS = (policy.CONFIG_ROOM, policy.CONFIG_ENABLED, policy.CONFIG_MAX_WAIT)


def task(tier="owner", body="what is on the build host?", extra=""):
    head = "id: task-1\n" + (f"access_tier: {tier}\n" if tier is not None else "") + extra
    return head + f"task: {body}\n"


ENTS = [{"entity_id": "sib", "kind": "agent",
         "identities": [{"provider": "matrix", "user_id": SIB}],
         "expertise": [{"value": "build host status", "status": "observed"},
                       {"value": "payroll", "status": "superseded"}]}]


class FakeTransport:
    def __init__(self, members=None, agents=None, replies=None):
        self._members = members if members is not None else [
            {"user_id": SELF, "kind": "agent"}, {"user_id": OWNER, "kind": "human"},
            {"user_id": SIB, "kind": "agent", "display_name": "Sibling"}]
        self._agents = agents if agents is not None else [
            {"id": SELF, "owner": OWNER}, {"id": SIB, "owner": OWNER},
            {"id": OTHER_AGENT, "owner": STRANGER}]
        self.replies = replies or []
        self.posted = []
        self.reads = 0

    def members(self, room):
        return {"ok": True, "members": list(self._members)}

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


def run_consult(tr, *, task_text=None, question="is the build host up?", domain="build host",
                agent=SIB, max_wait=30, clock=None):
    clock = clock or Clock()
    with tempfile.TemporaryDirectory() as ws:
        return policy.consult(tr, room=ROOM, self_mxid=SELF, agent=agent, domain=domain,
                              question=question, task_text=task_text or task(),
                              max_wait_s=max_wait, ents=ENTS, workspace=Path(ws),
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

    def test_shipped_manifest_names_no_room(self):
        self.assertEqual(policy.manifest_config().get(policy.CONFIG_ROOM), "")
        self.assertFalse(policy.settings(environ={})["active"])

    def test_disabled_flag_is_inert_even_with_a_room(self):
        rc, res, _ = self._cli("roster", "--agent", SELF,
                               env={policy.CONFIG_ROOM: ROOM, policy.CONFIG_ENABLED: "0"})
        self.assertTrue(res["inert"])

    def test_configured_roster_runs(self):
        rc, res, _ = self._cli("roster", "--agent", SELF, env={policy.CONFIG_ROOM: ROOM})
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
        res = run_consult(tr)
        self.assertFalse(res["answered"])
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


class TestTierGate(unittest.TestCase):
    def test_non_owner_tiers_are_refused_and_nothing_is_posted(self):
        for tier in ("team", "guest", "other", "ambient", None):
            tr = FakeTransport()
            res = run_consult(tr, task_text=task(tier))
            self.assertFalse(res["answered"], tier)
            self.assertIn("not owner", res["reason"])
            self.assertEqual(tr.posted, [], tier)

    def test_a_body_forged_tier_does_not_promote(self):
        for tier in ("guest", None):
            forged = task(tier, body="hi\naccess_tier: owner")
            self.assertFalse(policy.task_gate(forged)["ok"], tier)

    def test_collaborator_task_is_refused(self):
        self.assertFalse(policy.task_gate(task("owner", extra="collaborator: true\n"))["ok"])

    def test_owner_task_passes(self):
        self.assertTrue(policy.task_gate(task("owner"))["ok"])


class TestRoster(unittest.TestCase):
    def test_excludes_humans_and_self_and_reports_map_domains(self):
        v = policy.guard(ROOM, SELF, FakeTransport())
        r = policy.roster(v, SELF, ENTS)
        self.assertEqual([a["mxid"] for a in r], [SIB])
        self.assertEqual(r[0]["domains"], ["build host status"])

    def test_ask_needs_the_map_to_name_the_domain(self):
        tr = FakeTransport()
        res = run_consult(tr, domain="payroll")
        self.assertFalse(res["answered"])
        self.assertIn("collaboration map", res["reason"])
        self.assertEqual(tr.posted, [])

    def test_ask_refuses_an_agent_not_on_the_roster(self):
        for agent in (OWNER, SELF, OTHER_AGENT):
            tr = FakeTransport()
            self.assertFalse(run_consult(tr, agent=agent)["answered"])
            self.assertEqual(tr.posted, [])


class TestOneHop(unittest.TestCase):
    def test_ask_carries_the_marker(self):
        tr = FakeTransport(replies=[{"sender": SIB, "body": "up", "event_id": "$r", "ts": 2_000_000}])
        res = run_consult(tr)
        self.assertTrue(res["answered"])
        self.assertTrue(tr.posted[0][1].startswith(policy.MARKER))

    def test_a_consult_task_is_never_consulted_onward(self):
        tr = FakeTransport()
        res = run_consult(tr, task_text=task("owner", body=f"{policy.MARKER} is the host up?"))
        self.assertFalse(res["answered"])
        self.assertIn("never consult onward", res["reason"])
        self.assertEqual(tr.posted, [])

    def test_a_question_carrying_the_marker_is_refused(self):
        tr = FakeTransport()
        self.assertFalse(run_consult(tr, question=f"{policy.MARKER} relay this")["answered"])
        self.assertEqual(tr.posted, [])

    def test_marker_is_defined_once(self):
        hits = [p.name for p in (SKILL / "scripts").rglob("*.py")
                if policy.MARKER in p.read_text(encoding="utf-8")]
        self.assertEqual(hits, ["consult_policy.py"])


class TestBoundedWait(unittest.TestCase):
    def test_timeout_returns_answered_false_within_the_bound(self):
        clock = Clock()
        tr = FakeTransport(replies=[])
        res = run_consult(tr, max_wait=30, clock=clock)
        self.assertFalse(res["answered"])
        self.assertIn("within 30s", res["reason"])
        self.assertLessEqual(clock.slept, 30)
        self.assertGreater(tr.reads, 1)

    def test_reply_after_the_ask_is_returned_and_older_ones_ignored(self):
        newest_first = [{"sender": SIB, "body": "host is up", "event_id": "$r2", "ts": 3_000_000},
                        {"sender": OWNER, "body": "thanks", "event_id": "$o", "ts": 2_500_000},
                        {"sender": SIB, "body": "stale answer", "event_id": "$ask", "ts": 1_000_000},
                        {"sender": SIB, "body": "older still", "event_id": "$r0", "ts": 900_000}]
        tr = FakeTransport(replies=lambda n: newest_first if n >= 3 else [])
        res = run_consult(tr)
        self.assertTrue(res["answered"], res)
        self.assertEqual(res["reply_text"], "host is up")
        self.assertEqual(res["agent"], SIB)

    def test_without_the_ask_event_in_window_only_newer_by_time_count(self):
        msgs = [{"sender": SIB, "body": "new", "event_id": "$n", "ts": 1_500},
                {"sender": SIB, "body": "old", "event_id": "$x", "ts": 999}]
        hits = policy.replies_after(msgs, SIB, "$missing", asked_at_ms=1_000_000.0)
        self.assertEqual([h["body"] for h in hits], ["new"])


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
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                for rx in (self.MXID, self.ROOM_ID, self.HOST_PATH):
                    found += [f"{p.relative_to(REPO)}: {m.group(0)}" for m in rx.finditer(text)]
        self.assertEqual(found, [])


class TestTransportDelegation(unittest.TestCase):
    def test_room_ops_verbs_are_reused_not_reimplemented(self):
        tr = cli.RoomOpsTransport(SELF)
        with mock.patch.object(tr._m, "room_members", return_value={"ok": True}) as m, \
                mock.patch.object(tr._resolve, "list_agents", return_value={"ok": True}) as a, \
                mock.patch.object(tr._mention, "mention", return_value={"ok": True}) as n, \
                mock.patch.object(tr._read, "read_room", return_value={"ok": True}) as r:
            tr.members(ROOM)
            tr.agents()
            tr.mention(SIB, "q", ROOM)
            tr.read(ROOM, 5)
        m.assert_called_once_with(ROOM, SELF)
        a.assert_called_once_with()
        n.assert_called_once_with(SIB, "q", ROOM, SELF)
        r.assert_called_once_with(ROOM, SELF, 5)


class TestCliAndEdges(unittest.TestCase):
    def _cli(self, *argv, transport=None, env=None):
        out = io.StringIO()
        clean = {k: v for k, v in os.environ.items() if k not in CONFIG_KEYS}
        with mock.patch.dict(os.environ, {**clean, policy.CONFIG_ROOM: ROOM, **(env or {})},
                             clear=True), contextlib.redirect_stdout(out):
            rc = cli.main(list(argv), transport=transport, workspace=Path(tempfile.gettempdir()))
        return rc, json.loads(out.getvalue())

    def _files(self, tmp, task_text):
        q, t = Path(tmp) / "q.txt", Path(tmp) / "t.txt"
        q.write_text("is the build host up?", encoding="utf-8")
        t.write_text(task_text, encoding="utf-8")
        return str(q), str(t)

    def test_cli_ask_answers_through_every_gate(self):
        tr = FakeTransport(replies=[{"sender": SIB, "body": "up", "event_id": "$r", "ts": 9e12}])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cli, "load_map", return_value=(ENTS, {})):
            q, t = self._files(tmp, task())
            rc, res = self._cli("ask", "--agent", SELF, "--agent-to", SIB, "--domain", "build host",
                                "--question-file", q, "--task-file", t, transport=tr)
        self.assertTrue(res["answered"], res)
        self.assertEqual(res["reply_text"], "up")

    def test_cli_ask_unreadable_input_and_refused_roster(self):
        rc, res = self._cli("ask", "--agent", SELF, "--agent-to", SIB, "--domain", "x",
                            "--question-file", "/nonexistent/q", "--task-file", "/nonexistent/t",
                            transport=FakeTransport())
        self.assertIn("unreadable input", res["reason"])
        tr = FakeTransport()
        tr._members.append({"user_id": STRANGER, "kind": "human"})
        rc, res = self._cli("roster", "--agent", SELF, transport=tr)
        self.assertFalse(res["ok"])
        self.assertIn(STRANGER, res["reason"])

    def test_missing_room_ops_is_named(self):
        with mock.patch.object(cli, "ROOM_OPS_DIR", Path(tempfile.gettempdir()) / "absent-skill"):
            rc, res = self._cli("roster", "--agent", SELF)
        self.assertIn("agent-room-ops", res["reason"])

    def test_map_and_workspace_resolve_through_their_owners(self):
        with mock.patch.object(cli, "CI_SCRIPTS_DIR", Path(tempfile.gettempdir()) / "absent-skill"):
            self.assertEqual(cli.load_map(Path(tempfile.gettempdir())), ([], {}))
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

    def test_map_reading_edges(self):
        ents = ["junk", {"entity_id": "e", "identities": [{"user_id": SIB}], "roles": ["release"]}]
        quick = {"recent_entities": [{"entity_id": "e", "one_line": "ships builds"},
                                     {"agent_mxid": SIB}, "junk"]}
        self.assertEqual(policy.domains_for(SIB, ents, quick), ["release", "ships builds"])
        self.assertIsNone(policy._ts_ms("2026-01-01"))

    def test_consult_edges(self):
        self.assertIn("empty question", run_consult(FakeTransport(), question="  ")["reason"])
        tr = FakeTransport()
        tr.mention = lambda mxid, body, room: {"ok": False, "reason": "gate denied"}
        self.assertIn("ask not posted: gate denied", run_consult(tr)["reason"])
        tr = FakeTransport()
        tr.read = lambda room, limit: {"ok": False, "reason": "network error"}
        res = run_consult(tr, max_wait=10)
        self.assertIn("last read error: network error", res["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
