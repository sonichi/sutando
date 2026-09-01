#!/usr/bin/env python3
"""display.py room-scoped writes consult the same gate_allows allowlist the
gateway siblings do; profile writes touch only the agent's own field and skip
the room gate. Run: python3 tests/room-ops-display-gate.test.py"""
from __future__ import annotations

import importlib.util
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

    def test_profile_write_skips_the_room_gate(self):
        self._gate({"@other:test": {"all_member_rooms": True}})
        rc = display.main(["!room:test", "--profile", "--description", "d"])
        self.assertEqual(rc, 0)
        self.assertTrue(self._req_calls)


if __name__ == "__main__":
    unittest.main(verbosity=1)
