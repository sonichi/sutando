#!/usr/bin/env python3
"""room-ops reply citation — `--reply-to` on `say` and `mention`.

Lives in `tests/` rather than beside the module because the diff-coverage gate
discovers only `tests/*.test.py` (`scripts/coverage-gate.sh` -> `find tests -name
'*.test.py'`), the same reachability note `tests/room-ops-say.test.py` carries.

The load-bearing distinction: `reply_to` maps to `m.in_reply_to`, which is a
CITATION in the main timeline and NOT thread membership. Only a relation with
rel_type m.thread joins a thread, and the gateway has no field for that — so
these tests also pin that no thread surface is reachable from here. A call that
reported success while landing outside a requested thread is exactly the
silent-wrong-place failure the id check exists to prevent.

Run: python3 tests/room-ops-relations.test.py
"""
import pathlib
import sys
import unittest
from unittest import mock

_ROOM_OPS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_ROOM_OPS))
import mention as mn, relations as rl, room_ops, say as sy  # noqa: E401,E402

ROOM = "!r:ag2.space"
EV = "$evt1"
OTHER = "$evt2"
AGENTS = [{"id": "@peer:hs", "label": "peer"}]


def _seam(module, calls):
    """Patch a poster module's gateway seam; posted payloads land in `calls`."""
    return mock.patch.multiple(
        module,
        gateway=mock.Mock(return_value=("https://gw", {"h": "1"})),
        gate_allows=mock.Mock(return_value=True),
        load_gate=mock.Mock(return_value={}),
        http_json=lambda m, u, h, p: (calls.append(p), (200, {"event_id": "$e"}))[1],
    )


class RelationFieldsTests(unittest.TestCase):
    def test_no_citation_is_an_empty_dict(self):
        self.assertEqual(rl.relation_fields(), {})

    def test_empty_string_means_no_citation(self):
        self.assertEqual(rl.relation_fields(reply_to=""), {})

    def test_reply_to_becomes_the_wire_field(self):
        self.assertEqual(rl.relation_fields(reply_to=EV), {"reply_to": EV})

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(rl.relation_fields(reply_to="  $evt1  "), {"reply_to": EV})

    def test_malformed_ids_raise_rather_than_degrade(self):
        for bad in ("evt1", "$", "root", "   ", "e$vt"):
            with self.assertRaises(rl.RelationError):
                rl.relation_fields(reply_to=bad)

    def test_the_error_names_the_field_and_shows_the_value(self):
        with self.assertRaises(rl.RelationError) as ctx:
            rl.relation_fields(reply_to="evt1")
        self.assertIn("reply_to", str(ctx.exception))
        self.assertIn("evt1", str(ctx.exception))

    def test_no_thread_surface_is_offered(self):
        # The gateway cannot honour a thread relation, so asking for one must be
        # impossible rather than silently downgraded to a citation.
        with self.assertRaises(TypeError):
            rl.relation_fields(thread_root=EV)


class SayCitationTests(unittest.TestCase):
    def test_plain_say_cites_nothing(self):
        calls = []
        with _seam(sy, calls):
            self.assertTrue(sy.say("hi", ROOM, "@a:hs")["ok"])
        self.assertNotIn("reply_to", calls[0])

    def test_reply_to_rides_the_payload_and_leaves_the_rest_alone(self):
        calls = []
        with _seam(sy, calls):
            self.assertTrue(sy.say("hi", ROOM, "@a:hs", reply_to=EV)["ok"])
        self.assertEqual(calls[0]["reply_to"], EV)
        self.assertEqual(calls[0]["body"], "hi")
        self.assertEqual(calls[0]["op"], "message")

    def test_nothing_claims_thread_membership(self):
        calls = []
        with _seam(sy, calls):
            sy.say("hi", ROOM, "@a:hs", reply_to=EV)
        self.assertNotIn("thread_root", calls[0])
        self.assertNotIn("m.relates_to", calls[0])

    def test_bad_id_refuses_before_the_network(self):
        calls = []
        with _seam(sy, calls):
            res = sy.say("hi", ROOM, "@a:hs", reply_to="evt1")
        self.assertFalse(res["ok"])
        self.assertIn("event id", res["reason"])
        self.assertEqual(calls, [])   # the control below proves this seam fires

    def test_control_a_good_id_does_post(self):
        # Pairs with the refusal above: an empty call list only means "refused"
        # if the same seam demonstrably posts when the id is valid.
        calls = []
        with _seam(sy, calls):
            sy.say("hi", ROOM, "@a:hs", reply_to=EV)
        self.assertEqual(len(calls), 1)


class MentionCitationTests(unittest.TestCase):
    def test_citation_rides_alongside_the_mention(self):
        calls = []
        with _seam(mn, calls):
            res = mn.mention("peer", "ping", ROOM, "@a:hs", agents=AGENTS, reply_to=OTHER)
        self.assertTrue(res["ok"])
        self.assertEqual(calls[0]["reply_to"], OTHER)
        self.assertEqual(calls[0]["mentions"], ["@peer:hs"])

    def test_bad_id_refuses_before_resolve_and_network(self):
        calls = []
        with _seam(mn, calls):
            res = mn.mention("peer", "ping", ROOM, "@a:hs", agents=AGENTS, reply_to="root")
        self.assertFalse(res["ok"])
        self.assertIn("event id", res["reason"])
        self.assertEqual(calls, [])

    def test_control_a_good_id_does_post(self):
        calls = []
        with _seam(mn, calls):
            mn.mention("peer", "ping", ROOM, "@a:hs", agents=AGENTS, reply_to=OTHER)
        self.assertEqual(len(calls), 1)


class CitationCliTests(unittest.TestCase):
    def test_say_flag_reaches_say(self):
        with mock.patch.object(room_ops._say, "say",
                               return_value={"ok": True, "room_id": ROOM,
                                             "event_id": "$e", "reason": None}) as m:
            with mock.patch("sys.stdout"):
                rc = room_ops._main(["say", ROOM, "hi", "--reply-to", EV])
        self.assertEqual(rc, 0)
        m.assert_called_once_with("hi", ROOM, None, reply_to=EV)

    def test_say_worker_flag_is_forwarded(self):
        with mock.patch.object(room_ops._say, "say",
                               return_value={"ok": True, "room_id": ROOM,
                                             "event_id": "$e", "reason": None}) as m:
            with mock.patch("sys.stdout"):
                rc = room_ops._main(["say", ROOM, "hi", "--worker", "worker-9"])
        self.assertEqual(rc, 0)
        m.assert_called_once_with("hi", ROOM, None, reply_to=None, worker="worker-9")

    def test_mention_flag_reaches_mention(self):
        with mock.patch.object(room_ops._mention, "mention",
                               return_value={"ok": True, "room_id": ROOM, "mxid": "@p:hs",
                                             "event_id": "$e", "candidates": [],
                                             "reason": None}) as m:
            with mock.patch("sys.stdout"):
                rc = room_ops._main(["mention", "peer", "ping", ROOM, "--reply-to", EV])
        self.assertEqual(rc, 0)
        m.assert_called_once_with("peer", "ping", ROOM, None, reply_to=EV)

    def test_help_says_a_citation_is_not_a_thread(self):
        # The surface a caller reads first must carry the limitation, not only
        # the module docstring and the skill doc.
        import contextlib
        import io
        for verb in ("say", "mention"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                room_ops._main([verb, "--help"])
            self.assertIn("CITATION", out.getvalue())
            self.assertIn("does NOT put", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
