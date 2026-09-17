#!/usr/bin/env python3
"""display.py room-scoped writes consult the same gate_allows allowlist the
gateway siblings do; profile writes touch only the agent's own field and skip
the room gate. Run: python3 tests/room-ops-display-gate.test.py"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_SKILL))
_spec = importlib.util.spec_from_file_location("room_ops_display", _SKILL / "display.py")
display = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(display)


class DisplayGateBinds(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.update({"MATRIX_HS_URL": "http://hs.test",
                           "MATRIX_AS_TOKEN": "t", "AGENT_MXID": "@a:test"})
        self._gate_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._gate_dir.cleanup)
        self._req_calls = []
        self._real_req = display._req
        display._req = lambda *a, **k: (self._req_calls.append(a), (200, {}))[1]
        self.addCleanup(setattr, display, "_req", self._real_req)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _gate(self, doc):
        p = Path(self._gate_dir.name) / "gate.json"
        p.write_text(json.dumps(doc))
        os.environ["ROOM_OPS_GATE"] = str(p)

    def test_room_write_denied_by_gate_makes_no_request(self):
        self._gate({"@other:test": {"all_member_rooms": True}})
        rc = display.main(["!room:test", "--stripe", "on"])
        self.assertEqual(rc, 2)
        self.assertEqual(self._req_calls, [])

    def test_room_avatar_denied_by_gate_makes_no_request(self):
        self._gate({"@other:test": {"all_member_rooms": True}})
        rc = display.main(["!room:test", "--room-avatar", "mxc://hs/x"])
        self.assertEqual(rc, 2)
        self.assertEqual(self._req_calls, [])

    def test_room_write_allowed_when_gate_lists_the_room(self):
        self._gate({"@a:test": {"rooms": ["!room:test"]}})
        rc = display.main(["!room:test", "--stripe", "on"])
        self.assertEqual(rc, 0)
        self.assertTrue(self._req_calls)

    def test_profile_plus_room_avatar_is_rejected_with_zero_requests(self):
        # The pair is incoherent AND was the gate-bypass shape (review on 0df10599):
        # profile mode must never carry the room-scoped avatar write past the gate.
        self._gate({"@other:test": {"all_member_rooms": True}})
        rc = display.main(["!room:test", "--profile", "--room-avatar", "mxc://hs/x"])
        self.assertEqual(rc, 2)
        self.assertEqual(self._req_calls, [])
        self._gate({"@a:test": {"rooms": ["!room:test"]}})
        rc = display.main(["!room:test", "--profile", "--room-avatar", "mxc://hs/x"])
        self.assertEqual(rc, 2, "incompatible even when the gate allows")
        self.assertEqual(self._req_calls, [])

    def test_profile_write_skips_the_room_gate(self):
        self._gate({"@other:test": {"all_member_rooms": True}})
        rc = display.main(["!room:test", "--profile", "--description", "d"])
        self.assertEqual(rc, 0)
        self.assertTrue(self._req_calls)



class DisplayBranches(unittest.TestCase):
    """The rest of main() plus _req itself, so the whole module is exercised."""

    def setUp(self):
        self._env = dict(os.environ)
        os.environ.update({"MATRIX_HS_URL": "http://hs.test",
                           "MATRIX_AS_TOKEN": "t", "AGENT_MXID": "@a:test",
                           "ROOM_OPS_GATE": "/nonexistent-display-gate.json"})
        self._real_req = display._req
        self.addCleanup(setattr, display, "_req", self._real_req)
        display._req = lambda *a, **k: (200, {})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_missing_env_is_usage_error(self):
        os.environ["MATRIX_AS_TOKEN"] = ""
        self.assertEqual(display.main(["!r:test", "--stripe", "on"]), 2)

    def test_room_avatar_rejects_non_mxc(self):
        self.assertEqual(display.main(["!r:test", "--room-avatar", "https://x"]), 2)

    def test_room_avatar_puts_state(self):
        self.assertEqual(display.main(["!r:test", "--room-avatar", "mxc://hs/x"]), 0)

    def test_every_setter_lands_in_content(self):
        seen = {}
        display._req = lambda url, tok, method="GET", body=None: (
            seen.update({"body": body}) or (200, {}))
        rc = display.main(["!r:test", "--clear", "--base-color", "#112233",
                           "--corner", "tl", "--shape", "star",
                           "--worker-color", "w1=#445566", "--description", "d",
                           "--canonical-dm", "!dm:test", "--decorators", "auto",
                           "--message-style", "highlight",
                           "--highlight-weight", "strong"])
        self.assertEqual(rc, 0)
        c = seen["body"]
        self.assertEqual(c["baseColor"], "#112233")
        self.assertEqual(c["corner"], "tl")
        self.assertEqual(c["shape"], "star")
        self.assertEqual(c["colors"], {"w1": "#445566"})
        self.assertEqual(c["canonicalDm"], "!dm:test")
        self.assertEqual((c["decorators"], c["messageStyle"], c["highlightWeight"]),
                         ("auto", "highlight", "strong"))

    def test_bad_base_color_and_bad_worker_color_are_usage_errors(self):
        self.assertEqual(display.main(["!r:test", "--base-color", "red"]), 2)
        self.assertEqual(display.main(["!r:test", "--worker-color", "w1=red"]), 2)

    def test_worker_name_persists_and_blank_is_usage_error(self):
        seen = {}
        display._req = lambda url, tok, method="GET", body=None: (
            seen.update({"body": body}) or (200, {}))
        rc = display.main(["!r:test", "--clear", "--worker-name", "worker-1=Scout"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["body"]["names"], {"worker-1": "Scout"})
        self.assertEqual(display.main(["!r:test", "--worker-name", "worker-1=  "]), 2)

    def test_profile_merge_reads_existing_inner_document(self):
        def req(url, tok, method="GET", body=None):
            if method == "GET":
                return 200, {"space.ag2.identity": {"baseColor": "#000000"}}
            req.put = body
            return 200, {}
        display._req = req
        self.assertEqual(display.main(["!r:test", "--profile", "--description", "d"]), 0)
        self.assertEqual(req.put["space.ag2.identity"],
                         {"baseColor": "#000000", "description": "d"})


class ReqTransport(unittest.TestCase):
    def test_req_returns_status_and_parsed_body(self):
        class Resp:
            status = 200
            def read(self): return b'{"ok": true}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        real = display.urllib.request.urlopen
        display.urllib.request.urlopen = lambda *a, **k: Resp()
        try:
            self.assertEqual(display._req("http://x", "t", "PUT", {"a": 1}),
                             (200, {"ok": True}))
        finally:
            display.urllib.request.urlopen = real

    def test_req_http_error_yields_code_and_body(self):
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.HTTPError("http://x", 403, "forbidden", {},
                                         io.BytesIO(b'{"errcode": "M_FORBIDDEN"}'))
        real = display.urllib.request.urlopen
        display.urllib.request.urlopen = boom
        try:
            self.assertEqual(display._req("http://x", "t"),
                             (403, {"errcode": "M_FORBIDDEN"}))
        finally:
            display.urllib.request.urlopen = real


if __name__ == "__main__":
    unittest.main(verbosity=1)
