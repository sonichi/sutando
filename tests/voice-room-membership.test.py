#!/usr/bin/env python3
"""voice_room_membership: the verdict is the gateway's word, and it fails closed.

A session.context frame can name any well-formed room; the task bridge binds
a room only on a verdict this module writes. So: both identities joined →
verified; anything missing, unreadable or absent → refused. The file protocol
answers requests atomically and caches one gateway read per room per TTL.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import voice_room_membership as vrm  # noqa: E402

AGENT = "@agent:example.org"
OWNER = "@owner:example.org"
ROOM = "!room:example.org"


class VerdictTests(unittest.TestCase):
    def test_both_joined_is_the_only_pass(self):
        v = vrm.membership_verdict(ROOM, [AGENT, OWNER, "@x:example.org"], AGENT, OWNER, now=100.0)
        self.assertTrue(v["verified"])
        self.assertEqual((v["agent_joined"], v["owner_joined"], v["checked_at"], v["ttl_s"]),
                         (True, True, 100.0, vrm.VERDICT_TTL_S))
        self.assertEqual(v["reason"], "agent and owner joined")

    def test_refusals_name_their_reason(self):
        cases = [
            (dict(members=[AGENT], agent=AGENT, owner=OWNER), "owner not joined", True, False),
            (dict(members=[OWNER], agent=AGENT, owner=OWNER), "agent not joined", False, True),
            (dict(members=[], agent=AGENT, owner=OWNER), "agent not joined", False, False),
            (dict(members=None, agent=AGENT, owner=OWNER), "members unreadable", False, False),
            (dict(members=[AGENT, OWNER], agent="", owner=OWNER), "agent identity unknown", False, False),
            (dict(members=[AGENT, OWNER], agent=AGENT, owner=""), "owner identity unknown", False, False),
        ]
        for kw, reason, a, o in cases:
            v = vrm.membership_verdict(ROOM, **kw)
            self.assertFalse(v["verified"], reason)
            self.assertEqual(v["reason"], reason)
            self.assertEqual((v["agent_joined"], v["owner_joined"]), (a, o), reason)

    def test_a_non_room_id_is_refused_before_any_lookup(self):
        for bad in ("", "room", "#alias:s", "!nocolon", "! sp:ace"):
            v = vrm.membership_verdict(bad, [AGENT, OWNER], AGENT, OWNER)
            self.assertFalse(v["verified"], bad)
            self.assertEqual(v["reason"], "not a matrix room id")

    def test_verdict_defaults_now(self):
        v = vrm.membership_verdict(ROOM, [AGENT, OWNER], AGENT, OWNER)
        self.assertIsInstance(v["checked_at"], float)


class MembersFromRoomOpTests(unittest.TestCase):
    def test_member_list_shape(self):
        self.assertEqual(vrm.members_from_room_op(
            {"members": [{"user_id": AGENT, "display_name": "A"}, {"user_id": OWNER}, {"nope": 1}, "x", {"user_id": ""}]}),
            [AGENT, OWNER])

    def test_errors_and_foreign_shapes_are_none(self):
        for bad in ({"error": "members read failed (HTTP 403)"}, {"members": "x"}, {}, None, [], "s",
                    {"error": "x", "members": [{"user_id": AGENT}]}):
            self.assertIsNone(vrm.members_from_room_op(bad), repr(bad))


class FakeGateway:
    """Scripted identities + member rosters, counting gateway reads."""

    def __init__(self, rosters=None, agent=AGENT, owner=OWNER):
        self.rosters = rosters or {}
        self.agent = agent
        self.owner = owner
        self.reads = 0
        self.raise_members = None
        self.raise_identity = None

    def members(self, room):
        self.reads += 1
        if self.raise_members:
            raise self.raise_members
        return self.rosters.get(room)

    def agent_mxid(self):
        if self.raise_identity:
            raise self.raise_identity
        return self.agent

    def owner_mxid(self):
        return self.owner


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="voice-room-"))
        self.logs = []
        self.now = [1000.0]

    def make(self, gw, ttl=vrm.VERDICT_TTL_S):
        return vrm.RoomMembershipVerifier(self.tmp, gw.members, gw.agent_mxid, gw.owner_mxid,
                                          log=self.logs.append, ttl_s=ttl, clock=lambda: self.now[0])

    def test_verdict_caches_one_read_per_ttl(self):
        gw = FakeGateway({ROOM: [AGENT, OWNER]})
        v = self.make(gw)
        self.assertTrue(v.verified(ROOM))
        self.assertTrue(v.verified(ROOM))
        self.assertEqual(gw.reads, 1, "second answer came from the cache")
        gw.rosters[ROOM] = [AGENT]  # owner left
        self.assertTrue(v.verified(ROOM), "still cached inside the TTL")
        self.now[0] += vrm.VERDICT_TTL_S
        self.assertFalse(v.verified(ROOM), "re-read after the TTL sees the owner gone")
        self.assertEqual(gw.reads, 2)
        self.assertEqual(v.verdict(ROOM)["reason"], "owner not joined")

    def test_cache_is_keyed_on_room_agent_and_owner(self):
        gw = FakeGateway({ROOM: [AGENT, OWNER]})
        v = self.make(gw)
        self.assertTrue(v.verified(ROOM))
        gw.owner = "@other:example.org"  # the owner binding changed under a fresh cache entry
        self.assertFalse(v.verified(ROOM), "a new owner identity is never answered from the old read")
        self.assertEqual(v.verdict(ROOM)["reason"], "owner not joined")
        self.assertEqual(gw.reads, 2)
        gw.agent = "@reenrolled:example.org"
        self.assertFalse(v.verified(ROOM))
        self.assertEqual(gw.reads, 3, "a re-enrolled agent reads again too")
        self.assertEqual(set(v._cache), {(ROOM, AGENT, OWNER), (ROOM, AGENT, "@other:example.org"),
                                         (ROOM, "@reenrolled:example.org", "@other:example.org")})

    def test_expired_entries_are_evicted_on_write_and_the_map_is_capped(self):
        gw = FakeGateway({f"!r{i}:example.org": [AGENT, OWNER] for i in range(10)})
        v = self.make(gw)
        v._cache_max = 4
        for i in range(3):
            v.verified(f"!r{i}:example.org")
        self.assertEqual(len(v._cache), 3)
        self.now[0] += vrm.VERDICT_TTL_S
        v.verified("!r3:example.org")
        self.assertEqual(set(k[0] for k in v._cache), {"!r3:example.org"}, "expired entries leave on the next write")
        for i in range(4, 10):
            self.now[0] += 1
            v.verified(f"!r{i}:example.org")
        self.assertEqual(len(v._cache), 4, "never more than the cap")
        self.assertEqual(set(k[0] for k in v._cache), {f"!r{i}:example.org" for i in range(6, 10)}, "the oldest go first")
        self.assertEqual(vrm.RoomMembershipVerifier(self.tmp, gw.members, gw.agent_mxid, gw.owner_mxid, cache_max=0)._cache_max, 1)

    def test_forged_room_is_refused_and_a_refusal_is_cached_too(self):
        gw = FakeGateway({ROOM: [AGENT, OWNER]})
        v = self.make(gw)
        self.assertFalse(v.verified("!forged:example.org"))
        self.assertFalse(v.verified("!forged:example.org"))
        self.assertEqual(gw.reads, 1)
        self.assertEqual(v.verdict("!forged:example.org")["reason"], "members unreadable")

    def test_gateway_and_identity_failures_refuse_without_raising(self):
        gw = FakeGateway({ROOM: [AGENT, OWNER]})
        gw.raise_members = RuntimeError("HTTP 502")
        v = self.make(gw)
        self.assertFalse(v.verified(ROOM))
        self.assertTrue(any("members read failed" in line and "HTTP 502" in line for line in self.logs))
        gw2 = FakeGateway({ROOM: [AGENT, OWNER]})
        gw2.raise_identity = RuntimeError("no registry")
        v2 = self.make(gw2)
        self.assertEqual(v2.verdict(ROOM)["reason"], "agent identity unknown")
        self.assertEqual(gw2.reads, 0, "no member read without an identity")
        gw3 = FakeGateway({ROOM: [AGENT, OWNER]}, owner="")
        self.assertEqual(self.make(gw3).verdict(ROOM)["reason"], "owner identity unknown")
        gw4 = FakeGateway({ROOM: [AGENT, OWNER]}, agent=None)
        self.assertEqual(self.make(gw4).verdict(ROOM)["reason"], "agent identity unknown")

    def test_non_list_and_non_string_members_are_tolerated(self):
        gw = FakeGateway({ROOM: "not a list"})
        self.assertEqual(self.make(gw).verdict(ROOM)["reason"], "members unreadable")
        gw = FakeGateway({ROOM: [AGENT, 7, OWNER, None]})
        self.assertTrue(self.make(gw).verified(ROOM))

    def _request(self, key, body):
        self.tmp.mkdir(parents=True, exist_ok=True)
        path = self.tmp / f"{key}{vrm.REQUEST_SUFFIX}"
        path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        return path

    def test_service_once_answers_requests_atomically_and_removes_them(self):
        gw = FakeGateway({ROOM: [AGENT, OWNER]})
        v = self.make(gw)
        ok_req = self._request("room-ok", {"room_id": ROOM})
        forged_req = self._request("room-forged", {"room_id": "!forged:example.org"})
        self.assertEqual(v.service_once(), 2)
        self.assertFalse(ok_req.exists())
        self.assertFalse(forged_req.exists())
        ok = json.loads((self.tmp / f"room-ok{vrm.VERDICT_SUFFIX}").read_text())
        forged = json.loads((self.tmp / f"room-forged{vrm.VERDICT_SUFFIX}").read_text())
        self.assertEqual((ok["room_id"], ok["verified"]), (ROOM, True))
        self.assertEqual((forged["room_id"], forged["verified"], forged["reason"]),
                         ("!forged:example.org", False, "members unreadable"))
        self.assertFalse(list(self.tmp.glob("*.tmp")), "no temp file left behind")
        self.assertTrue(any("REFUSED" in line for line in self.logs))
        self.assertTrue(any("verified" in line and ROOM in line for line in self.logs))
        self.assertEqual(v.service_once(), 0)

    def test_malformed_requests_are_dropped_not_answered(self):
        gw = FakeGateway({ROOM: [AGENT, OWNER]})
        v = self.make(gw)
        bad = [self._request("not-json", "{nope"),
               self._request("no-room", {"x": 1}),
               self._request("bad-room", {"room_id": "room"}),
               self._request("bad key!", {"room_id": ROOM}),
               self._request("list", [ROOM])]
        self.assertEqual(v.service_once(), 0)
        for p in bad:
            self.assertFalse(p.exists(), p.name)
        self.assertFalse(list(self.tmp.glob("*" + vrm.VERDICT_SUFFIX)))
        self.assertEqual(gw.reads, 0)

    def test_service_once_survives_a_missing_dir_and_an_unwritable_verdict(self):
        gw = FakeGateway({ROOM: [AGENT, OWNER]})
        v = vrm.RoomMembershipVerifier(self.tmp / "absent", gw.members, gw.agent_mxid, gw.owner_mxid,
                                       log=self.logs.append)
        self.assertEqual(v.service_once(), 0)

        class UnlistableDir:
            def glob(self, pattern):
                raise OSError("EIO")
        v.check_dir = UnlistableDir()
        self.assertEqual(v.service_once(), 0, "an unlistable dir answers nothing and raises nothing")
        v2 = self.make(gw)
        req = self._request("room-ok", {"room_id": ROOM})
        original = vrm.write_verdict
        vrm.write_verdict = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            self.assertEqual(v2.service_once(), 0)
        finally:
            vrm.write_verdict = original
        self.assertTrue(req.exists(), "the request stays for the next pass")
        self.assertTrue(any("could not write verdict" in line for line in self.logs))

    def test_run_loop_answers_within_one_poll_and_stops(self):
        gw = FakeGateway({ROOM: [AGENT, OWNER]})
        v = self.make(gw)
        stop = threading.Event()
        t = v.start(stop, poll_s=0.02)
        req = self._request("room-ok", {"room_id": ROOM})
        deadline = 200
        while req.exists() and deadline:
            deadline -= 1
            stop.wait(0.01)
        self.assertFalse(req.exists(), "the loop serviced the request")
        stop.set()
        t.join(timeout=2)
        self.assertFalse(t.is_alive())

    def test_run_loop_logs_a_failing_pass_and_keeps_going(self):
        gw = FakeGateway({ROOM: [AGENT, OWNER]})
        v = self.make(gw)
        calls = []

        def boom():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("one bad pass")
            return 0
        v.service_once = boom
        stop = threading.Event()
        t = v.start(stop, poll_s=0.01)
        while len(calls) < 2:
            stop.wait(0.005)
        stop.set()
        t.join(timeout=2)
        self.assertTrue(any("verifier pass failed: one bad pass" in line for line in self.logs))

    def test_write_verdict_replaces_atomically(self):
        target = self.tmp / "x.verdict.json"
        vrm.write_verdict(target, {"a": 1})
        vrm.write_verdict(target, {"a": 2})
        self.assertEqual(json.loads(target.read_text()), {"a": 2})
        self.assertFalse((self.tmp / "x.verdict.json.tmp").exists())

    def tearDown(self):
        for p in sorted(self.tmp.rglob("*"), reverse=True):
            try:
                p.unlink() if p.is_file() else p.rmdir()
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
