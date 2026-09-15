#!/usr/bin/env python3
"""`mention` must be able to post ONE message that structurally mentions several
agents at once — the only room-ops call that can, since `say` never sets
`mentions` by design (say.py) and this backend only routes an agent-authored
message to another agent via `m.mentions` (receiver.py: "Only direct structured
mentions can trigger a peer. Body text... must not fan out work" — the body-text
fallback in `bridge_core.addressed_agents` is HUMAN-sender only).

Found live 2026-09-15: a "tag the whole fleet" room message was posted with `say`
and several `@handle:ag2.space` strings written into the body by hand. It read
correctly to a human in scrollback; it created zero real mentions for any of the
named agents, because `say` posts no `mentions` field and an agent-authored body
is never text-scanned for peers. `mention` with a list of handles is the fix:
resolve each handle independently (same pipeline as the single-handle case),
refuse the WHOLE call — posting nothing — if any handle fails, and otherwise
post once with every resolved mxid in `mentions` and leading the body.
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


ROOM = "!r:ag2.space"
A = "@sutando-a.agent:ag2.space"
B = "@sutando-b.agent:ag2.space"
C = "@sutando-c.agent:ag2.space"


class TestBuildBodyMulti(unittest.TestCase):
    def setUp(self):
        self.M = _load("mention")

    def test_single_string_is_unchanged(self):
        self.assertEqual(self.M.build_body(A, "hi"), f"{A} — hi")

    def test_list_of_one_matches_string_form(self):
        self.assertEqual(self.M.build_body([A], "hi"), self.M.build_body(A, "hi"))

    def test_multiple_mxids_lead_space_separated(self):
        self.assertEqual(self.M.build_body([A, B, C], "review this"),
                         f"{A} {B} {C} — review this")

    def test_empty_message_omits_the_dash(self):
        self.assertEqual(self.M.build_body([A, B], ""), f"{A} {B}")


class TestMentionMulti(unittest.TestCase):
    """Real mxids as "handles" throughout: a bare mxid short-circuits
    resolve_user, keeping every test focused on the multi-handle glue rather
    than re-proving single-handle resolution (already covered by
    agent-room-ops-mention-room-fallback.test.py)."""

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
        self.M.resolve_in_room = lambda *a, **k: {
            "ok": False, "mxid": None, "candidates": [], "reason": "no agent matches",
            "unsupported": False}

    def _mention(self, handles):
        return self.M.mention(handles, "ping", ROOM, "@me:ag2.space", agents=[])

    def test_two_real_mxids_post_once_with_both_mentioned(self):
        got = self._mention([A, B])
        self.assertTrue(got["ok"], got)
        self.assertEqual(len(self.posted), 1, "must post exactly once, not per handle")
        self.assertEqual(self.posted[0]["mentions"], [A, B])
        self.assertTrue(self.posted[0]["body"].startswith(f"{A} {B}"))
        self.assertEqual(got["mxids"], [A, B])
        self.assertEqual(got["mxid"], A, "mxid stays the first, for single-handle callers")

    def test_single_string_handle_still_posts_a_one_element_mentions_list(self):
        got = self._mention(A)
        self.assertTrue(got["ok"], got)
        self.assertEqual(self.posted[0]["mentions"], [A])
        self.assertEqual(got["mxids"], [A])
        self.assertEqual(got["mxid"], A)

    def test_one_bad_handle_among_several_posts_nothing(self):
        mod = type(sys)("members")
        mod.room_members = lambda room_id, agent_mxid=None: {
            "ok": False, "members": [], "reason": "no gateway configured"}
        with mock.patch.dict(sys.modules, {"members": mod}):
            got = self.M.mention([A, "nobody-matches-this", B], "ping", ROOM,
                                 "@me:ag2.space", agents=[])
        self.assertFalse(got["ok"])
        self.assertEqual(self.posted, [], "a partial mention must never post")

    def test_failures_names_every_bad_handle_not_just_the_first(self):
        # No real member/network dependency: an unreadable roster (ok:False)
        # is enough to make every unresolved handle fail deterministically.
        mod = type(sys)("members")
        mod.room_members = lambda room_id, agent_mxid=None: {
            "ok": False, "members": [], "reason": "no gateway configured"}
        with mock.patch.dict(sys.modules, {"members": mod}):
            got = self.M.mention(["nobody-1", A, "nobody-2"], "ping", ROOM,
                                 "@me:ag2.space", agents=[])
        self.assertFalse(got["ok"])
        bad_handles = {f["handle"] for f in got["failures"]}
        self.assertEqual(bad_handles, {"nobody-1", "nobody-2"},
                         "both unresolved handles must be named, not just nobody-1")
        # Backward-compatible single-handle fields describe the FIRST failure.
        self.assertEqual(got["reason"], got["failures"][0]["reason"])

    def test_empty_list_is_a_refusal_not_a_crash(self):
        got = self.M.mention([], "ping", ROOM, "@me:ag2.space")
        self.assertFalse(got["ok"])
        self.assertEqual(got["reason"], "handle required")
        self.assertEqual(self.posted, [])

    def test_blank_handles_in_the_list_are_dropped_not_treated_as_failures(self):
        # argparse nargs="+" cannot itself hand back an empty string, but a
        # programmatic caller might build the list dynamically.
        got = self._mention([A, "", B])
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mxids"], [A, B])

    def test_roster_fallback_is_fetched_once_across_several_handles(self):
        """Two handles that both need the room-roster fallback must trigger
        exactly one `room_members` read, not one per handle."""
        reads = []

        def room_members(room_id, agent_mxid=None):
            reads.append(room_id)
            return {"ok": True, "members": [
                {"user_id": A, "display_name": "Agent A", "kind": "agent"},
                {"user_id": B, "display_name": "Agent B", "kind": "agent"},
            ], "reason": None}

        mod = type(sys)("members")
        mod.room_members = room_members
        mod.classify_member = _load("members").classify_member
        with mock.patch.dict(sys.modules, {"members": mod}):
            got = self._mention(["agent a", "agent b"])
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mxids"], [A, B])
        self.assertEqual(reads, [ROOM], "exactly one roster read for two handles")


if __name__ == "__main__":
    unittest.main()
