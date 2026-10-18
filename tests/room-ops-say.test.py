#!/usr/bin/env python3
"""room-ops `say` — post plain text into a room without mentioning anyone.

Lives in `tests/` rather than beside the module because the diff-coverage gate
discovers only `tests/*.test.py` (`scripts/coverage-gate.sh` -> `find tests -name
'*.test.py'`), the same reachability note `tests/room-ops-grant.test.py` carries.

The load-bearing assertion is that the posted body is the caller's string
VERBATIM. `mention` welds a resolved mxid onto the front of every body so the
peer's matcher fires; a `say` that kept doing that would silently ping someone on
every status line, and the bug would be invisible from the call site.

Run: python3 tests/room-ops-say.test.py
"""
import os
import pathlib
import sys
import unittest
from unittest import mock

_ROOM_OPS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_ROOM_OPS))
import say as sy  # noqa: E402

ROOM = "!r:ag2.space"
OPEN_GATE = {}


def _capture(status=200, parsed=None, raises=None):
    """Patch say's gateway seam; return the list that collects posted payloads."""
    calls = []

    def fake_http_json(method, url, headers, payload):
        calls.append({"method": method, "url": url, "payload": payload})
        if raises is not None:
            raise raises
        return status, (parsed if parsed is not None else {"event_id": "$evt"})

    ctx = mock.patch.multiple(
        sy,
        gateway=mock.Mock(return_value=("https://gw", {"h": "1"})),
        gate_allows=mock.Mock(return_value=True),
        load_gate=mock.Mock(return_value=OPEN_GATE),
        http_json=fake_http_json,
    )
    return ctx, calls


class SayPostsVerbatimTests(unittest.TestCase):
    def test_body_is_the_message_unprefixed(self):
        ctx, calls = _capture()
        with ctx:
            res = sy.say("deploy finished, 3 green", ROOM, agent_mxid="@me:hs")
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(calls), 1)
        payload = calls[0]["payload"]
        # THE point of the subcommand: byte-for-byte, no mxid, no separator.
        self.assertEqual(payload["body"], "deploy finished, 3 green")
        self.assertEqual(payload["op"], "message")
        self.assertEqual(payload["room_id"], ROOM)
        self.assertEqual(res["event_id"], "$evt")

    def test_sends_no_mentions_key(self):
        ctx, calls = _capture()
        with ctx:
            sy.say("status line", ROOM, agent_mxid="@me:hs")
        # Present-but-empty would still be a behavioural claim to the broker;
        # `say` makes none.
        self.assertNotIn("mentions", calls[0]["payload"])

    def test_an_mxid_inside_the_message_is_not_re_prefixed(self):
        """A caller who writes an mxid owns it; say must not add a second one."""
        ctx, calls = _capture()
        with ctx:
            sy.say("@qingyun:ag2.space asked about this", ROOM, agent_mxid="@me:hs")
        self.assertEqual(calls[0]["payload"]["body"],
                         "@qingyun:ag2.space asked about this")

    def test_multiline_body_survives_intact(self):
        ctx, calls = _capture()
        with ctx:
            sy.say("line one\n\nline three", ROOM, agent_mxid="@me:hs")
        self.assertEqual(calls[0]["payload"]["body"], "line one\n\nline three")


class SayRefusesBeforePostingTests(unittest.TestCase):
    """Every refusal must happen with ZERO requests issued — a refusal that still
    posts is worse than no refusal, because the room already saw it."""

    def test_missing_room_id(self):
        ctx, calls = _capture()
        with ctx:
            res = sy.say("hello", "", agent_mxid="@me:hs")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "room_id required")
        self.assertEqual(calls, [])

    def test_empty_message(self):
        ctx, calls = _capture()
        with ctx:
            res = sy.say("", ROOM, agent_mxid="@me:hs")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "message required")
        self.assertEqual(calls, [])

    def test_whitespace_only_message(self):
        ctx, calls = _capture()
        with ctx:
            res = sy.say("   \n\t ", ROOM, agent_mxid="@me:hs")
        self.assertFalse(res["ok"])
        self.assertEqual(calls, [])

    def test_client_gate_denial_refuses_without_posting(self):
        """`say` inherits mention's authorization boundary — it does not widen it."""
        calls = []
        with mock.patch.multiple(
            sy,
            gateway=mock.Mock(return_value=("https://gw", {})),
            gate_allows=mock.Mock(return_value=False),
            load_gate=mock.Mock(return_value=OPEN_GATE),
            http_json=mock.Mock(side_effect=AssertionError("posted despite gate denial")),
        ):
            res = sy.say("should not appear", ROOM, agent_mxid="@me:hs")
        self.assertFalse(res["ok"])
        self.assertIn("gate denied", res["reason"])
        self.assertEqual(calls, [])

    def test_no_gateway_configured(self):
        with mock.patch.multiple(
            sy,
            gateway=mock.Mock(return_value=(None, {})),
            gate_allows=mock.Mock(return_value=True),
            load_gate=mock.Mock(return_value=OPEN_GATE),
        ):
            res = sy.say("hello", ROOM, agent_mxid="@me:hs")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "no gateway configured")


class SayAgentIdentityTests(unittest.TestCase):
    def test_agent_mxid_falls_back_to_the_environment(self):
        """Callers omit --agent constantly; the gate must still see an identity."""
        seen = {}
        ctx, _calls = _capture()
        with ctx, mock.patch.dict(os.environ, {"AGENT_MXID": "@env-agent:hs"}):
            sy.gate_allows.side_effect = lambda mxid, room, gate: seen.setdefault("mxid", mxid) or True
            sy.say("hello", ROOM)
        self.assertEqual(seen["mxid"], "@env-agent:hs")


class SayCliDispatchTests(unittest.TestCase):
    def test_room_ops_say_routes_to_say(self):
        """Pins the CLI wiring: `room_ops say <room> <msg>` reaches say.say."""
        import io
        import room_ops
        from contextlib import redirect_stdout
        with mock.patch.object(room_ops._say, "say",
                               return_value={"ok": True, "room_id": ROOM,
                                             "event_id": "$e", "reason": None}) as m:
            with redirect_stdout(io.StringIO()):
                rc = room_ops._main(["say", ROOM, "hi there"])
        self.assertEqual(rc, 0)
        # say() takes (message, room_id); the CLI takes (room_id, message).
        # reply_to rides as a keyword and is None when --reply-to is not given.
        m.assert_called_once_with("hi there", ROOM, None, reply_to=None)


class SayNetworkFailureTests(unittest.TestCase):
    def test_http_error_degrades_and_does_not_raise(self):
        ctx, _calls = _capture(raises=sy.HTTPError("u", 403, "forbidden", None, None))
        with ctx:
            res = sy.say("hello", ROOM, agent_mxid="@me:hs")
        self.assertFalse(res["ok"])
        self.assertTrue(res["reason"])

    def test_url_error_is_reported_not_raised(self):
        ctx, _calls = _capture(raises=sy.URLError("boom"))
        with ctx:
            res = sy.say("hello", ROOM, agent_mxid="@me:hs")
        self.assertFalse(res["ok"])
        self.assertIn("network error", res["reason"])

    def test_confirmed_only_when_an_event_id_comes_back(self):
        ctx, _calls = _capture(parsed={"event_id": "$evt"})
        with ctx:
            res = sy.say("hello", ROOM, agent_mxid="@me:hs")
        self.assertEqual(res["state"], "confirmed")
        self.assertEqual(res["event_id"], "$evt")

    def test_200_without_proof_is_unconfirmed_not_confirmed(self):
        """The gateway can answer 200 having swallowed a send failure. Reporting
        that as confirmed lets a caller drop its fallback and lose the reply."""
        for body in ({}, {"ok": True}, ["not", "a", "dict"], None):
            with self.subTest(body=body):
                ctx, _calls = _capture(parsed=body if body is not None else {})
                with ctx:
                    res = sy.say("hello", ROOM, agent_mxid="@me:hs")
                self.assertEqual(res["state"], "unconfirmed")
                self.assertIsNone(res["event_id"])
                self.assertIn("not confirmed", res["reason"])
                # ok stays true: flipping it would make callers re-send a
                # delivered message, which cron-notify deliberately avoids.
                self.assertTrue(res["ok"])




class A2uiCardEnvTests(unittest.TestCase):
    """SUTANDO_WORKER_A2UI attaches a VALIDATED buttons card; anything that
    fails validation attaches nothing rather than riding the wire malformed."""

    _GOOD = ('{"type": "buttons", "prompt": "Pick one",'
             ' "options": [{"label": "A", "action": "choose A"}]}')

    def _card_sent(self, env_value):
        ctx, calls = _capture()
        env = {"SUTANDO_WORKER_A2UI": env_value, "SUTANDO_CORE_ID": "1"}
        with ctx, mock.patch.dict(os.environ, env, clear=False):
            sy.say("hi", ROOM, "@a:hs")
        self.assertEqual(len(calls), 1)
        return calls[0]["payload"].get("extra_content", {}).get("space.ag2.a2ui")

    def test_valid_card_rides_extra_content_normalized(self):
        card = self._card_sent(self._GOOD)
        self.assertEqual(card, {"version": "0.9", "type": "buttons",
                                "prompt": "Pick one",
                                "options": [{"label": "A", "action": "choose A"}]})

    def test_unparseable_json_attaches_nothing(self):
        self.assertIsNone(self._card_sent("{not json"))

    def test_wrong_type_attaches_nothing(self):
        self.assertIsNone(self._card_sent('{"type": "modal", "options": []}'))

    def test_option_missing_action_attaches_nothing(self):
        self.assertIsNone(self._card_sent(
            '{"type": "buttons", "options": [{"label": "A"}]}'))

    def test_empty_options_attaches_nothing(self):
        self.assertIsNone(self._card_sent('{"type": "buttons", "options": []}'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
