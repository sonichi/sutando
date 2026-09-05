#!/usr/bin/env python3
"""`mention` must reach a peer agent that the /v1/agents directory does not list.

Measured live 2026-09-03: `resolve_user('sutando-sonichi')` returns
`no agent matches`, while `@sutando-sonichi:ag2.space` IS a member of the room
being posted to. So the correct tool for an @-mention could not mention the
peers most worth mentioning, and callers fell back to `say`, which sends no
`mentions` and pings nobody.
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

SKILL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "skills", "agent-room-ops")
sys.path.insert(0, SKILL)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SKILL, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMatchMember(unittest.TestCase):
    def setUp(self):
        self.R = _load("resolve")

    def test_exact_localpart_resolves(self):
        got = self.R.match_member("sutando-sonichi",
                                  ["@sutando-sonichi:ag2.space", "@chi:ag2.space"])
        self.assertTrue(got["ok"])
        self.assertEqual(got["mxid"], "@sutando-sonichi:ag2.space")

    def test_substring_resolves_when_unique(self):
        got = self.R.match_member("sonichi", ["@sutando-sonichi:ag2.space", "@chi:ag2.space"])
        self.assertEqual(got["mxid"], "@sutando-sonichi:ag2.space")

    def test_two_members_matching_is_ambiguous_not_a_guess(self):
        got = self.R.match_member(
            "sutando", ["@sutando-rui:ag2.space", "@sutando-sonichi:ag2.space"])
        self.assertFalse(got["ok"])
        self.assertEqual(len(got["candidates"]), 2)

    def test_a_non_member_does_not_resolve(self):
        got = self.R.match_member("sutando-rui", ["@sutando-sonichi:ag2.space"])
        self.assertFalse(got["ok"])

    def test_empty_membership_is_a_miss_not_a_crash(self):
        self.assertFalse(self.R.match_member("anyone", [])["ok"])
        self.assertFalse(self.R.match_member("anyone", None)["ok"])


class TestMentionFallback(unittest.TestCase):
    """The fallback is exercised through `mention`, since the ORDER is the
    contract: directory first, room only on an unambiguous miss."""

    def setUp(self):
        self.M = _load("mention")
        self.posted = []

        def fake_http_json(method, url, headers, payload):
            self.posted.append(payload)
            return 200, {"ok": True, "event_id": "$evt"}

        self.M.http_json = fake_http_json
        self.M.gateway = lambda: ("https://relay", {})
        self.M.gate_allows = lambda *a, **k: True
        self.M.load_gate = lambda *a, **k: {}

    def _members(self, ids, ok=True):
        mod = type(sys)("members")
        mod.room_members = lambda room_id, agent_mxid=None: {
            "ok": ok, "members": list(ids), "reason": None}
        return mock.patch.dict(sys.modules, {"members": mod})

    def test_directory_miss_resolves_from_the_room(self):
        with self._members(["@sutando-sonichi:ag2.space", "@chi:ag2.space"]):
            got = self.M.mention("sutando-sonichi", "ping", "!r:ag2.space",
                                 "@me:ag2.space", agents=[])
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mxid"], "@sutando-sonichi:ag2.space")
        self.assertEqual(self.posted[0]["mentions"], ["@sutando-sonichi:ag2.space"])
        self.assertTrue(self.posted[0]["body"].startswith("@sutando-sonichi:ag2.space"))

    def test_the_directory_still_wins_when_it_can_answer(self):
        """The room must not override a directory hit — otherwise a same-named
        room member would silently take a resolved agent's place."""
        with self._members(["@sutando-sonichi:other.server"]):
            got = self.M.mention("sutando-sonichi", "ping", "!r:ag2.space", "@me:ag2.space",
                                 agents=[{"id": "@sutando-sonichi:ag2.space"}])
        self.assertEqual(got["mxid"], "@sutando-sonichi:ag2.space")

    def test_an_ambiguous_directory_answer_is_not_widened(self):
        """Ambiguity means too many, so consulting a second source can only turn
        a refusal into a guess. It must stay a refusal."""
        agents = [{"id": "@sutando-a:ag2.space"}, {"id": "@sutando-b:ag2.space"}]
        with self._members(["@sutando-a:ag2.space"]):
            got = self.M.mention("sutando", "ping", "!r:ag2.space", "@me:ag2.space",
                                 agents=agents)
        self.assertFalse(got["ok"])
        self.assertEqual(len(got["candidates"]), 2)
        self.assertEqual(self.posted, [])

    def test_an_unreadable_member_list_keeps_the_directory_reason(self):
        """A membership read that failed is not evidence of non-membership; the
        caller must not be told the handle is absent from a room nobody read.

        The stub returns a list that WOULD match: an empty one is satisfied by
        both the guarded and unguarded code, so it discriminates nothing."""
        with self._members(["@sutando-sonichi:ag2.space"], ok=False):
            got = self.M.mention("sutando-sonichi", "ping", "!r:ag2.space", "@me:ag2.space",
                                 agents=[])
        self.assertFalse(got["ok"])
        self.assertIn("no agent matches", got["reason"])
        self.assertEqual(self.posted, [])

    def test_an_unimportable_members_module_keeps_the_directory_reason(self):
        """`members` reaches the network, so it is imported lazily inside the
        fallback — and a lazy import can fail. That must read as "cannot answer",
        never as "the handle is not in the room"."""
        with mock.patch.dict(sys.modules, {"members": None}):
            got = self.M.mention("sutando-sonichi", "ping", "!r:ag2.space", "@me:ag2.space",
                                 agents=[])
        self.assertFalse(got["ok"])
        self.assertIn("no agent matches", got["reason"])
        self.assertEqual(self.posted, [])

    def test_a_room_miss_reports_and_posts_nothing(self):
        with self._members(["@chi:ag2.space"]):
            got = self.M.mention("sutando-rui", "ping", "!r:ag2.space", "@me:ag2.space",
                                 agents=[])
        self.assertFalse(got["ok"])
        self.assertEqual(self.posted, [])

    def test_two_room_members_matching_refuses_rather_than_picking(self):
        with self._members(["@sutando-rui:ag2.space", "@sutando-sonichi:ag2.space"]):
            got = self.M.mention("sutando", "ping", "!r:ag2.space", "@me:ag2.space",
                                 agents=[])
        self.assertFalse(got["ok"])
        self.assertEqual(len(got["candidates"]), 2)
        self.assertEqual(self.posted, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
