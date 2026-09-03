#!/usr/bin/env python3
"""room-ops events — a 401/403 carries the gateway's own error text to the caller.

Lives in `tests/` because the diff-coverage gate discovers only `tests/*.test.py`
(`scripts/coverage-gate.sh` -> `find tests -name '*.test.py'`); the skill-local
suite covers degrade_reason_from() itself, this file is what the gate sees for
the three events.py call sites — subscribe (_op_call), pull, stream.

Regression: a 403 on events_subscribe rendered as "not a joined member" while
the gateway had answered `{"error": "platform grant events.subscribe missing"}`
— a different subsystem entirely. Each site must surface BOTH the local
diagnosis and the server's text; a non-auth status uses the server's text.

Run: python3 tests/room-ops-events-server-reason.test.py
"""
import io
import os
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

_ROOM_OPS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_ROOM_OPS))
import events as ev  # noqa: E402

ROOM = "!r:ag2.space"
AGENT = "@a:ag2.space"
GRANT_403 = b'{"error": "permission denied: platform grant events.subscribe missing"}'


def _http_error(code, body):
    return urllib.error.HTTPError("https://r/v1/room", code, "err", {}, io.BytesIO(body))


class _EnvCase(unittest.TestCase):
    """Pins the gateway POSITIVELY: the resolver reads several env vars plus a
    vault fallback, so scrubbing names can leave a live path and a real call."""

    def setUp(self):
        self._g = mock.patch.object(ev, "gateway", return_value=("https://r", {}))
        self._g.start()
        self.addCleanup(self._g.stop)
        self._saved = os.environ.get("ROOM_OPS_GATE")
        os.environ.pop("ROOM_OPS_GATE", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["ROOM_OPS_GATE"] = self._saved


class SubscribeTests(_EnvCase):
    def test_403_carries_local_diagnosis_and_server_text(self):
        with mock.patch.object(ev, "http_json", side_effect=_http_error(403, GRANT_403)):
            res = ev.subscribe(ROOM, ["message.created"], agent_mxid=AGENT, gate=None)
        self.assertFalse(res["ok"])
        self.assertIn("not a joined member", res["reason"])
        self.assertIn("platform grant events.subscribe missing", res["reason"])

    def test_401_keeps_the_token_diagnosis_in_front(self):
        body = b'{"error": "denied - agent not a joined member"}'
        with mock.patch.object(ev, "http_json", side_effect=_http_error(401, body)):
            res = ev.subscribe(ROOM, ["message.created"], agent_mxid=AGENT, gate=None)
        self.assertTrue(res["reason"].startswith("auth failed"))
        self.assertIn("(server said: denied - agent not a joined member)", res["reason"])

    def test_bodiless_403_is_the_plain_status_text(self):
        with mock.patch.object(ev, "http_json", side_effect=_http_error(403, b"")):
            res = ev.subscribe(ROOM, ["message.created"], agent_mxid=AGENT, gate=None)
        self.assertEqual(res["reason"], "denied — agent not a joined member (403)")


class PullTests(_EnvCase):
    def test_403_carries_local_diagnosis_and_server_text(self):
        with mock.patch.object(ev, "_http_get_json", side_effect=_http_error(403, GRANT_403)):
            res = ev.pull(cursor=5)
        self.assertFalse(res["ok"])
        self.assertEqual(res["cursor"], 5)
        self.assertIn("not a joined member", res["reason"])
        self.assertIn("platform grant events.subscribe missing", res["reason"])

    def test_non_auth_status_uses_the_servers_text(self):
        body = b'{"error": "events cursor 5 expired"}'
        with mock.patch.object(ev, "_http_get_json", side_effect=_http_error(410, body)):
            res = ev.pull(cursor=5)
        self.assertEqual(res["reason"], "events cursor 5 expired")


class StreamTests(_EnvCase):
    def test_403_open_raises_with_local_diagnosis_and_server_text(self):
        with mock.patch.object(ev, "_open_stream", side_effect=_http_error(403, GRANT_403)):
            with self.assertRaises(RuntimeError) as cm:
                ev.stream(on_event=lambda c, e: None)
        self.assertIn("not a joined member", str(cm.exception))
        self.assertIn("platform grant events.subscribe missing", str(cm.exception))

    def test_transient_status_is_still_a_disconnect(self):
        # The body is only consulted on the permission statuses; a 502 must keep
        # raising StreamDisconnected so the resume wrapper reconnects.
        body = b'{"error": "upstream reset"}'
        with mock.patch.object(ev, "_open_stream", side_effect=_http_error(502, body)):
            with self.assertRaises(ev.StreamDisconnected):
                ev.stream(on_event=lambda c, e: None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
