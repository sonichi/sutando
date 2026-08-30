#!/usr/bin/env python3
"""Framing is per-transport (docs/scp-v0.md section 2), pinned executably.

Unix socket: newline-delimited — readline() hands parse_line one record per
line. WebSocket: exactly ONE JSON-RPC object per text frame — two newline-
joined records in a single frame are a parse error (-32700), not two requests.

Run: python3 tests/runtime-api-ws-framing.test.py
Exit: 0 on pass, 1 on fail.
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

import protocol  # noqa: E402
import ws_transport as wst  # noqa: E402

REQ = {"jsonrpc": "2.0", "id": 1, "method": "sutando.info", "params": {}}
REQ2 = {"jsonrpc": "2.0", "id": 2, "method": "sutando.status", "params": {}}


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_str(self, s):
        self.sent.append(json.loads(s))


class _FakeDispatcher:
    def __init__(self):
        self.calls = []

    async def handle(self, method, params):
        self.calls.append(method)
        return {"ok": True}


def _transport(dispatcher):
    t = wst.WsTransport.__new__(wst.WsTransport)
    t.dispatcher = dispatcher
    t.device_store = None
    t.voice_bridge = None
    t._log = lambda *_: None
    return t


def _dispatch(t, ws, data):
    auth = {"kind": "shared", "token": "x",
            "grants": frozenset({"sutando.info", "sutando.status"})}
    asyncio.run(t._dispatch_one(ws, None, auth, set(), data))


class ParseLineFraming(unittest.TestCase):
    def test_one_object_parses(self):
        rid, method, params = protocol.parse_line(json.dumps(REQ).encode())
        self.assertEqual((rid, method), (1, "sutando.info"))

    def test_two_ndjson_records_in_one_buffer_are_a_parse_error(self):
        raw = (json.dumps(REQ) + "\n" + json.dumps(REQ2)).encode()
        with self.assertRaises(protocol.ProtocolError) as cm:
            protocol.parse_line(raw)
        self.assertEqual(cm.exception.code, -32700)

    def test_unix_side_per_line_framing_is_fine(self):
        # readline() splits; each line parses independently — the NDJSON contract.
        for i, obj in enumerate((REQ, REQ2), 1):
            rid, _, _ = protocol.parse_line((json.dumps(obj) + "\n").encode())
            self.assertEqual(rid, i)


class WsDispatchFraming(unittest.TestCase):
    def test_one_text_frame_dispatches_once(self):
        d = _FakeDispatcher(); ws = _FakeWs()
        _dispatch(_transport(d), ws, json.dumps(REQ))
        self.assertEqual(d.calls, ["sutando.info"])
        self.assertEqual(ws.sent[0]["id"], 1)

    def test_two_records_in_one_frame_dispatch_nothing(self):
        d = _FakeDispatcher(); ws = _FakeWs()
        _dispatch(_transport(d), ws, json.dumps(REQ) + "\n" + json.dumps(REQ2))
        self.assertEqual(d.calls, [], "joined frame must not dispatch")
        self.assertEqual(ws.sent[0]["error"]["code"], -32700)

    def test_negative_control_presplit_frames_do_dispatch(self):
        # Proves the assertion above can fail: split client-side and both run.
        d = _FakeDispatcher(); ws = _FakeWs()
        t = _transport(d)
        for obj in (REQ, REQ2):
            _dispatch(t, ws, json.dumps(obj))
        self.assertEqual(d.calls, ["sutando.info", "sutando.status"])


if __name__ == "__main__":
    r = unittest.main(exit=False).result
    sys.exit(0 if r.wasSuccessful() else 1)
