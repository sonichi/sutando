#!/usr/bin/env python3
"""room-ops `events emit` — send one typed space.ag2.* TIMELINE event as this agent.

Lives in `tests/` rather than beside the module because the diff-coverage gate
discovers only `tests/*.test.py` (`scripts/coverage-gate.sh` -> `find tests -name
'*.test.py'`), the same reachability note `tests/room-ops-say.test.py` carries.
The skill-local suite covers the same behaviour; this file is what the gate sees.

Two load-bearing assertions:

  1. The accepted TYPE NAMESPACE is the server's rule and is not re-encoded in
     the client — a copy would drift silently. `test_server_refusal_verbatim`
     fails if someone adds a client-side namespace check that shortcuts the
     round trip, because the server's wording would stop reaching the caller.
  2. `emit` reads its receipt through `receipt.classify`, the same three-state
     reading `say` and `mention` share. UNCONFIRMED must stay `ok:true` so a
     caller cannot re-send an event the gateway already landed.

Run: python3 tests/room-ops-events-emit.test.py
"""
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

_ROOM_OPS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_ROOM_OPS))
import events as ev  # noqa: E402
import room_ops as ro  # noqa: E402

ROOM = "!r:ag2.space"
AGENT = "@a:ag2.space"
ETYPE = "space.ag2.app.card"


class _EnvCase(unittest.TestCase):
    """Pins the gateway POSITIVELY instead of unsetting env names. The resolver
    reads several vars plus a vault fallback, so name-scrubbing can leave a live
    path and a "no gateway" test then makes a real network call."""

    def setUp(self):
        self._g = mock.patch.object(ev, "gateway", return_value=("https://r", {}))
        self._g.start()
        self.addCleanup(self._g.stop)
        self._saved = os.environ.get("ROOM_OPS_GATE")
        os.environ.pop("ROOM_OPS_GATE", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["ROOM_OPS_GATE"] = self._saved


class EmitTests(_EnvCase):
    def test_no_gateway_configured(self):
        with mock.patch.object(ev, "gateway", return_value=(None, {})):
            res = ev.emit(ROOM, ETYPE, {"k": 1}, agent_mxid=AGENT, gate=None)
        self.assertFalse(res["ok"])
        self.assertIn("no gateway", res["reason"])

    def test_gate_denial_is_key_complete(self):
        # A denial returns through the SHARED _result builder, so an emit
        # caller reading res["event_id"] gets None rather than a KeyError.
        res = ev.emit(ROOM, ETYPE, {"k": 1}, agent_mxid=AGENT, gate={})
        self.assertFalse(res["ok"])
        self.assertIn("gate denied", res["reason"])
        self.assertIsNone(res["event_id"])
        self.assertIsNone(res["state"])

    def test_envelope_and_confirmed_receipt(self):
        cap = {}
        with mock.patch.object(
            ev, "http_json",
            side_effect=lambda m, u, h, p: (cap.update(url=u, payload=p), (200, {"event_id": "$e1"}))[1],
        ):
            res = ev.emit(ROOM, ETYPE, {"k": 1}, agent_mxid=AGENT, gate=None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["event_id"], "$e1")
        self.assertEqual(res["state"], "confirmed")
        self.assertTrue(cap["url"].endswith("/v1/room"))
        self.assertEqual(cap["payload"],
                         {"op": "event", "room_id": ROOM, "type": ETYPE, "content": {"k": 1}})

    def test_unconfirmed_stays_ok(self):
        with mock.patch.object(ev, "http_json", side_effect=lambda m, u, h, p: (200, {"ok": True})):
            res = ev.emit(ROOM, ETYPE, {"k": 1}, agent_mxid=AGENT, gate=None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["state"], "unconfirmed")
        self.assertIsNone(res["event_id"])

    def test_server_refusal_verbatim(self):
        refusal = {"error": "event type must be under space.ag2.*"}
        with mock.patch.object(ev, "http_json", side_effect=lambda m, u, h, p: (200, refusal)):
            res = ev.emit(ROOM, "not.ag2.thing", {"k": 1}, agent_mxid=AGENT, gate=None)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], refusal["error"])


class _Args:
    def __init__(self, **kw):
        self.events_cmd = "emit"
        self.room_id = ROOM
        self.type = ETYPE
        self.content = json.dumps({"k": 1})
        self.agent_mxid = AGENT
        self.__dict__.update(kw)


class DispatchTests(_EnvCase):
    def test_bad_json_is_structured_not_raised(self):
        res = ro._dispatch_events(_Args(content="not-json"))
        self.assertFalse(res["ok"])
        self.assertIn("not valid JSON", res["reason"])

    def test_non_object_content_rejected(self):
        for body in ("[1,2]", '"str"', "7"):
            res = ro._dispatch_events(_Args(content=body))
            self.assertFalse(res["ok"], body)
            self.assertIn("must be a JSON object", res["reason"])

    def test_delegates_to_events_emit(self):
        with mock.patch.object(ro._events, "emit", return_value={"ok": True}) as m:
            ro._dispatch_events(_Args())
        m.assert_called_once_with(ROOM, ETYPE, {"k": 1}, agent_mxid=AGENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
