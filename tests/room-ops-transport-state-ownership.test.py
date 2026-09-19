#!/usr/bin/env python3
"""Every room-write caller reads a transport failure the same way.

A timeout can be raised AFTER the POST commits, so it is ambiguous, never proven
non-delivery: `failed` licenses a retry that duplicates a message the room already
has. `receipt.py` owns that rule; this pins that `say`, `mention` and `emit` all
delegate to it instead of each deciding at its own `except`.

The 4xx row is the control: it must stay FAILED everywhere, so a test that passed
by making everything UNKNOWN would fail here.

Run: python3 tests/room-ops-transport-state-ownership.test.py
"""
import pathlib
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

_ROOM_OPS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_ROOM_OPS))
import events as ev  # noqa: E402
import mention as mn  # noqa: E402

import receipt as _receipt  # noqa: E402
import say as sy  # noqa: E402

ROOM = "!r:ag2.space"
MXID = "@peer:ag2.space"


def _http_403():
    return HTTPError("https://gw/v1/room", 403, "Forbidden", {}, None)


def _raiser(exc):
    def fake_http_json(method, url, headers, payload):
        raise exc
    return fake_http_json


def _say(exc):
    with mock.patch.multiple(
        sy,
        gateway=mock.Mock(return_value=("https://gw", {})),
        gate_allows=mock.Mock(return_value=True),
        load_gate=mock.Mock(return_value={}),
        http_json=_raiser(exc),
    ):
        return sy.say("hi", ROOM, agent_mxid="@me:hs")


def _mention(exc):
    with mock.patch.multiple(
        mn,
        gateway=mock.Mock(return_value=("https://gw", {})),
        gate_allows=mock.Mock(return_value=True),
        load_gate=mock.Mock(return_value={}),
        resolve_user=mock.Mock(return_value={"ok": True, "mxid": MXID}),
        http_json=_raiser(exc),
    ):
        return mn.mention("peer", "hi", ROOM, agent_mxid="@me:hs")


def _emit(exc):
    with mock.patch.multiple(
        ev,
        gateway=mock.Mock(return_value=("https://gw", {})),
        gate_allows=mock.Mock(return_value=True),
        load_gate=mock.Mock(return_value={}),
        http_json=_raiser(exc),
    ):
        return ev.emit(ROOM, "note", {"a": 1}, agent_mxid="@me:hs")


CALLERS = (("say", _say), ("mention", _mention), ("emit", _emit))


class TransportStateOwnershipTests(unittest.TestCase):
    def _row(self, exc):
        return {name: fn(exc).get("state") for name, fn in CALLERS}

    def test_timeout_is_unknown_for_every_caller(self):
        row = self._row(TimeoutError("timed out"))
        self.assertEqual(
            row, {n: _receipt.UNKNOWN for n, _ in CALLERS},
            f"a timeout may have committed the write; got {row}")

    def test_url_error_is_unknown_for_every_caller(self):
        row = self._row(URLError("connection reset"))
        self.assertEqual(
            row, {n: _receipt.UNKNOWN for n, _ in CALLERS},
            f"a transport error is ambiguous, not proven failure; got {row}")

    def test_definite_4xx_stays_failed_for_every_caller(self):
        # Control: without this an all-UNKNOWN implementation would pass above.
        row = self._row(_http_403())
        self.assertEqual(
            row, {n: _receipt.FAILED for n, _ in CALLERS},
            f"a 403 is a definite refusal and must not park; got {row}")

    def test_no_caller_reports_a_transport_failure_as_confirmed(self):
        for exc in (TimeoutError("t"), URLError("u"), _http_403()):
            for name, fn in CALLERS:
                res = fn(exc)
                self.assertFalse(res["ok"], f"{name} on {type(exc).__name__}")
                self.assertNotEqual(res.get("state"), _receipt.CONFIRMED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
