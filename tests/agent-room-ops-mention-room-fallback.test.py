#!/usr/bin/env python3
"""`mention` must reach a peer agent that the /v1/agents directory does not list.

Measured live 2026-09-03: `resolve_user('sutando-sonichi')` returns
`no agent matches`, while `@sutando-sonichi:ag2.space` IS a member of the room
being posted to. So the correct tool for an @-mention could not mention the
peers most worth mentioning, and callers fell back to `say`, which sends no
`mentions` and pings nobody.

2026-09-10: the room fallback gained the broker's own resolver (`op:
resolve_user`, display names included) ahead of the member list, and the
member list now carries its display names into the match — so "Bassil's
Sutando", the way the room shows the peer, resolves too. The ORDER is the
contract: directory, then broker (the handle, then its platform slug), then
roster — and an ambiguous answer from any of them is a refusal that no later
source widens into a guess.
"""
import importlib.util
import os
import sys
import unittest
import urllib.error
from unittest import mock

SKILL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "skills", "agent-room-ops")
sys.path.insert(0, SKILL)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SKILL, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASSIL_AGENT = "@bassil-bassil-s-sutando.agent:ag2.space"
SONICHI = "@sutando-sonichi:ag2.space"
ROOM = "!r:ag2.space"


class _Broker:
    """Stand-in for resolve.resolve_in_room: an answer per query (unlisted
    queries get `default`), and a record of what was asked in which order — so
    a test can pin WHAT was asked and WHETHER, not only the outcome."""
    NOT_FOUND = {"ok": False, "mxid": None, "display_name": "", "candidates": [],
                 "reason": "user not found in room", "unsupported": False}
    UNSUPPORTED = {**NOT_FOUND, "reason": "unknown op resolve_user", "unsupported": True}
    NETWORK = {**NOT_FOUND, "reason": "network error: timed out"}

    @staticmethod
    def hit(mxid, name=""):
        return {"ok": True, "mxid": mxid, "display_name": name, "candidates": [],
                "reason": None, "unsupported": False}

    @staticmethod
    def ambiguous(*mxids):
        return {**_Broker.NOT_FOUND, "candidates": list(mxids),
                "reason": "ambiguous: " + ", ".join(mxids)}

    def __init__(self, default=None, answers=None):
        self.default = default or self.NOT_FOUND
        self.answers = dict(answers or {})
        self.asked = []

    def __call__(self, query, room_id):
        self.asked.append((query, room_id))
        return dict(self.answers.get(query, self.default))


class TestMatchMember(unittest.TestCase):
    def setUp(self):
        self.R = _load("resolve")

    def test_exact_localpart_resolves(self):
        got = self.R.match_member("sutando-sonichi", [SONICHI, "@chi:ag2.space"])
        self.assertTrue(got["ok"])
        self.assertEqual(got["mxid"], SONICHI)

    def test_substring_resolves_when_unique(self):
        got = self.R.match_member("sonichi", [SONICHI, "@chi:ag2.space"])
        self.assertEqual(got["mxid"], SONICHI)

    def test_two_members_matching_is_ambiguous_not_a_guess(self):
        got = self.R.match_member("sutando", ["@sutando-rui:ag2.space", SONICHI])
        self.assertFalse(got["ok"])
        self.assertEqual(len(got["candidates"]), 2)

    def test_a_non_member_does_not_resolve(self):
        got = self.R.match_member("sutando-rui", [SONICHI])
        self.assertFalse(got["ok"])

    def test_empty_membership_is_a_miss_not_a_crash(self):
        self.assertFalse(self.R.match_member("anyone", [])["ok"])
        self.assertFalse(self.R.match_member("anyone", None)["ok"])

    def test_member_dicts_resolve_by_display_name(self):
        # The shape `members.room_members` returns; the name is what the bare
        # mxid list never had.
        roster = [{"user_id": "@bassil:ag2.space", "display_name": "Bassil", "kind": "human"},
                  {"user_id": BASSIL_AGENT, "display_name": "Bassil's Sutando", "kind": "agent"}]
        self.assertEqual(self.R.match_member("Bassil's Sutando", roster)["mxid"], BASSIL_AGENT)
        self.assertEqual(self.R.match_member("bassil", roster)["mxid"], "@bassil:ag2.space")

    def test_substring_tiers_below_token_prefix(self):
        """A fragment that starts no token still resolves when unique: first
        as a substring of the localpart, then of a display name."""
        self.assertEqual(self.R.match_member("onichi", [SONICHI, "@chi:ag2.space"])["mxid"],
                         SONICHI)
        roster = [{"user_id": "@bassil:ag2.space", "display_name": "Bassil"},
                  {"user_id": BASSIL_AGENT, "display_name": "Bassil's Sutando"}]
        self.assertEqual(self.R.match_member("assil's sut", roster)["mxid"], BASSIL_AGENT)

    def test_a_query_that_normalises_to_nothing_is_a_miss(self):
        for q in ("'s", "@", "  -_-  "):
            got = self.R.match_member(q, [SONICHI])
            self.assertFalse(got["ok"], q)
            self.assertIn("no agent matches", got["reason"], q)

    def test_token_prefix_needs_tokens_on_both_sides(self):
        self.assertFalse(self.R._token_prefix("", "sutando-sonichi"))
        self.assertFalse(self.R._token_prefix("sonichi", ""))
        self.assertTrue(self.R._token_prefix("bassil-sutando", "bassil-s-sutando"))

    def test_a_full_mxid_is_trusted_and_fetches_no_directory(self):
        got = self.R.match_member("@whoever:ag2.space", [SONICHI])
        self.assertEqual((got["ok"], got["mxid"]), (True, "@whoever:ag2.space"))
        self.R.list_agents = lambda: self.fail("a full mxid must not fetch /v1/agents")
        self.assertEqual(self.R.resolve_user("@whoever:ag2.space")["mxid"], "@whoever:ag2.space")

    def test_directory_entries_without_an_id_are_skipped(self):
        got = self.R.match_agent("sonichi", [{"label": "Sonichi's Sutando"}, "junk", {"id": SONICHI}])
        self.assertEqual(got["mxid"], SONICHI)

    def test_resolve_user_reads_the_directory_only_when_no_agents_are_given(self):
        self.R.list_agents = lambda: {"ok": False, "agents": [], "reason": "no gateway configured"}
        got = self.R.resolve_user("sonichi")
        self.assertEqual((got["ok"], got["reason"]), (False, "no gateway configured"))
        self.R.list_agents = lambda: {"ok": True, "agents": [{"id": SONICHI}], "reason": None}
        self.assertEqual(self.R.resolve_user("sonichi")["mxid"], SONICHI)
        self.assertEqual(self.R.resolve_user("sonichi", agents=[])["ok"], False)

    def test_an_unimportable_members_module_leaves_a_tie_a_tie(self):
        """The agent preference reads `members.classify_member` lazily; when that
        import fails nobody is known to be an agent, so the tie stays a refusal."""
        pair = [{"id": "@alex:ag2.space", "display_name": "Alex Sutando"},
                {"id": "@alex-alex-sutando.agent:ag2.space", "display_name": "Alex Sutando"}]
        with mock.patch.dict(sys.modules, {"members": None}):
            got = self.R.match_agent("Alex Sutando", pair, prefer_agents=True)
        self.assertFalse(got["ok"])
        self.assertEqual(len(got["candidates"]), 2)


class TestMentionFallback(unittest.TestCase):
    """The fallback is exercised through `mention`, since the ORDER is the
    contract: directory first, broker only on an unambiguous miss, roster only
    when the broker could not answer."""

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
        # The broker knows nobody unless a test tells it otherwise, so the
        # pre-broker cases below reach the roster exactly as they always did.
        self.broker = _Broker()
        self.M.resolve_in_room = self.broker
        self.member_reads = []
        self.classify = _load("members").classify_member

    def _members(self, members, ok=True):
        mod = type(sys)("members")

        def room_members(room_id, agent_mxid=None):
            self.member_reads.append(room_id)
            return {"ok": ok, "members": list(members), "reason": None}

        mod.room_members = room_members
        mod.classify_member = self.classify   # the tie-break reads it lazily
        return mock.patch.dict(sys.modules, {"members": mod})

    def _mention(self, handle, agents=None):
        return self.M.mention(handle, "ping", ROOM, "@me:ag2.space",
                              agents=[] if agents is None else agents)

    # ----- the roster path (2026-09-03) ----- #
    def test_directory_miss_resolves_from_the_room(self):
        with self._members([SONICHI, "@chi:ag2.space"]):
            got = self._mention("sutando-sonichi")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mxid"], SONICHI)
        self.assertEqual(got["resolved_by"], "room")
        self.assertEqual(self.posted[0]["mentions"], [SONICHI])
        self.assertTrue(self.posted[0]["body"].startswith(SONICHI))

    def test_the_directory_still_wins_when_it_can_answer(self):
        """The room must not override a directory hit — otherwise a same-named
        room member would silently take a resolved agent's place. Neither the
        broker nor the roster is even asked."""
        self.broker.default = _Broker.hit("@sutando-sonichi:other.server")
        with self._members(["@sutando-sonichi:other.server"]):
            got = self._mention("sutando-sonichi", agents=[{"id": SONICHI}])
        self.assertEqual(got["mxid"], SONICHI)
        self.assertEqual(got["resolved_by"], "directory")
        self.assertEqual(self.broker.asked, [])
        self.assertEqual(self.member_reads, [])

    def test_an_ambiguous_directory_answer_is_not_widened(self):
        """Ambiguity means too many, so consulting a second source can only turn
        a refusal into a guess. It must stay a refusal."""
        agents = [{"id": "@sutando-a:ag2.space"}, {"id": "@sutando-b:ag2.space"}]
        self.broker.default = _Broker.hit("@sutando-a:ag2.space")
        with self._members(["@sutando-a:ag2.space"]):
            got = self._mention("sutando", agents=agents)
        self.assertFalse(got["ok"])
        self.assertEqual(len(got["candidates"]), 2)
        self.assertEqual(self.posted, [])
        self.assertEqual(self.broker.asked, [])
        self.assertEqual(self.member_reads, [])

    def test_an_unreadable_member_list_keeps_the_directory_reason(self):
        """A membership read that failed is not evidence of non-membership; the
        caller must not be told the handle is absent from a room nobody read.

        The stub returns a list that WOULD match: an empty one is satisfied by
        both the guarded and unguarded code, so it discriminates nothing."""
        with self._members([SONICHI], ok=False):
            got = self._mention("sutando-sonichi")
        self.assertFalse(got["ok"])
        self.assertIn("no agent matches", got["reason"])
        self.assertEqual(self.posted, [])

    def test_an_unimportable_members_module_keeps_the_directory_reason(self):
        """`members` reaches the network, so it is imported lazily inside the
        fallback — and a lazy import can fail. That must read as "cannot answer",
        never as "the handle is not in the room"."""
        with mock.patch.dict(sys.modules, {"members": None}):
            got = self._mention("sutando-sonichi")
        self.assertFalse(got["ok"])
        self.assertIn("no agent matches", got["reason"])
        self.assertEqual(self.posted, [])

    def test_a_room_miss_reports_and_posts_nothing(self):
        with self._members(["@chi:ag2.space"]):
            got = self._mention("sutando-rui")
        self.assertFalse(got["ok"])
        self.assertEqual(self.posted, [])

    def test_two_room_members_matching_refuses_rather_than_picking(self):
        with self._members(["@sutando-rui:ag2.space", SONICHI]):
            got = self._mention("sutando")
        self.assertFalse(got["ok"])
        self.assertEqual(len(got["candidates"]), 2)
        self.assertEqual(self.posted, [])

    # ----- the broker path (2026-09-10) ----- #
    def test_broker_hit_posts_with_mentions_and_the_mxid_leading(self):
        self.broker.answers["Bassil's Sutando"] = _Broker.hit(BASSIL_AGENT, "Bassil's Sutando")
        with self._members(["@chi:ag2.space"]):
            got = self._mention("Bassil's Sutando")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mxid"], BASSIL_AGENT)
        self.assertEqual(got["resolved_by"], "broker")
        self.assertEqual(self.posted[0]["mentions"], [BASSIL_AGENT])
        self.assertTrue(self.posted[0]["body"].startswith(BASSIL_AGENT))
        self.assertEqual(self.broker.asked, [("Bassil's Sutando", ROOM)])
        # The roster is the LAST resort, not a second opinion on a hit.
        self.assertEqual(self.member_reads, [])

    def test_broker_ambiguity_refuses_and_consults_nobody_else(self):
        self.broker.default = _Broker.ambiguous(BASSIL_AGENT, SONICHI)
        with self._members([{"user_id": BASSIL_AGENT, "display_name": "Bassil's Sutando"}]):
            got = self._mention("sutando")
        self.assertFalse(got["ok"])
        self.assertEqual(sorted(got["candidates"]), sorted([BASSIL_AGENT, SONICHI]))
        self.assertIn("ambiguous", got["reason"])
        self.assertEqual(self.posted, [])
        self.assertEqual(self.member_reads, [])
        self.assertEqual(len(self.broker.asked), 1)   # too-many is not retried with the slug

    def test_broker_without_the_op_falls_back_to_display_names(self):
        self.broker.default = _Broker.UNSUPPORTED
        roster = [{"user_id": "@bassil:ag2.space", "display_name": "Bassil", "kind": "human"},
                  {"user_id": BASSIL_AGENT, "display_name": "Bassil's Sutando", "kind": "agent"},
                  {"user_id": SONICHI, "display_name": "Sonichi's Sutando", "kind": "agent"}]
        with self._members(roster):
            got = self._mention("Bassil's Sutando")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mxid"], BASSIL_AGENT)
        self.assertEqual(got["resolved_by"], "room")
        self.assertEqual(self.posted[0]["mentions"], [BASSIL_AGENT])
        self.assertEqual(self.member_reads, [ROOM])
        self.assertEqual(len(self.broker.asked), 1)   # a missing op is not retried with the slug

    def test_broker_miss_on_the_name_but_hit_on_its_slug(self):
        """The broker normalises nothing, and a display name can be changed
        while the localpart keeps the registration slug: "Susan's bot" then
        misses, but its platform spelling "susan-s-bot" is a substring of
        `@liususan091219-susan-s-bot.agent:ag2.space`."""
        susan = "@liususan091219-susan-s-bot.agent:ag2.space"
        self.broker.answers["susan-s-bot"] = _Broker.hit(susan, "Susan's assistant")
        with self._members(["@chi:ag2.space"]):
            got = self._mention("Susan's bot")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mxid"], susan)
        self.assertEqual(got["resolved_by"], "broker")
        self.assertEqual([q for q, _ in self.broker.asked], ["Susan's bot", "susan-s-bot"])
        self.assertEqual(self.member_reads, [])

    def test_no_slug_retry_when_the_handle_already_is_its_slug(self):
        with self._members([SONICHI]):
            self._mention("sutando-sonichi")
        self.assertEqual([q for q, _ in self.broker.asked], ["sutando-sonichi"])

    def test_broker_network_error_still_reaches_the_roster(self):
        self.broker.default = _Broker.NETWORK
        with self._members([SONICHI]):
            got = self._mention("sutando-sonichi")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["resolved_by"], "room")

    def test_broker_network_error_and_unreadable_roster_keep_the_directory_reason(self):
        self.broker.default = _Broker.NETWORK
        with self._members([SONICHI], ok=False):
            got = self._mention("sutando-sonichi")
        self.assertFalse(got["ok"])
        self.assertIn("no agent matches", got["reason"])
        self.assertEqual(self.posted, [])

    # ----- a resolved mxid that still cannot be posted ----- #
    def test_client_gate_denial_names_the_resolved_mxid_and_posts_nothing(self):
        self.M.gate_allows = lambda *a, **k: False
        got = self._mention("sutando-sonichi", agents=[{"id": SONICHI}])
        self.assertFalse(got["ok"])
        self.assertIn("client gate denied", got["reason"])
        self.assertEqual((got["mxid"], got["resolved_by"]), (SONICHI, "directory"))
        self.assertEqual(self.posted, [])

    def test_no_gateway_is_named_after_the_resolve(self):
        self.M.gateway = lambda: ("", {})
        got = self._mention("sutando-sonichi", agents=[{"id": SONICHI}])
        self.assertFalse(got["ok"])
        self.assertEqual(got["reason"], "no gateway configured")
        self.assertEqual(got["mxid"], SONICHI)
        self.assertEqual(self.posted, [])

    def test_post_failures_degrade_with_a_reason_and_the_mxid(self):
        """An HTTP or transport failure on the post itself never raises: the
        result keeps the resolved mxid so the caller knows WHO was not reached."""
        for exc, marker in ((urllib.error.HTTPError("https://relay/v1/room", 403, "x", {}, None),
                             "403"),
                            (urllib.error.URLError("dns"), "network error"),
                            (TimeoutError("slow"), "network error")):
            def _boom(*a, **k):
                raise exc
            self.M.http_json = _boom
            got = self._mention("sutando-sonichi", agents=[{"id": SONICHI}])
            self.assertFalse(got["ok"], exc)
            self.assertIn(marker, got["reason"], exc)
            self.assertEqual((got["mxid"], got["resolved_by"]), (SONICHI, "directory"), exc)
        self.assertEqual(self.posted, [])

    def test_roster_tie_between_a_person_and_their_agent_picks_the_agent(self):
        # A mention is a hand-off, and the agent is the one that acts on it.
        self.broker.default = _Broker.UNSUPPORTED
        roster = [{"user_id": "@alex:ag2.space", "display_name": "Alex Sutando"},
                  {"user_id": "@alex-alex-sutando.agent:ag2.space", "display_name": "Alex Sutando"}]
        with self._members(roster):
            got = self._mention("Alex Sutando")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mxid"], "@alex-alex-sutando.agent:ag2.space")
        self.assertEqual(got["resolved_by"], "room")


if __name__ == "__main__":
    unittest.main(verbosity=2)
