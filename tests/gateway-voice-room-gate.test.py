#!/usr/bin/env python3
"""The loader's voice-room wiring: gateway readers, the owner cache, and the
claim gate that holds an unverified room's result before any claim.

Loads src/remote-gateway-bridge.py in-process (the exec'd namespace gives the
loader its `_req`, `_reenroll_identity` and `_proactive_route`) with a fake
gateway in `_req`, so every branch is driven without a network or a process.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "remote-gateway-bridge.py"
AGENT = "@agent:example.org"
OWNER = "@owner:example.org"
OK_ROOM = "!ok:example.org"
FORGED_ROOM = "!forged:example.org"


@contextlib.contextmanager
def _load(env: dict, name: str):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(name, _SRC)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.rosters = {OK_ROOM: [AGENT, OWNER]}
        self.owner = OWNER

    def req(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, payload))
        if path == "/v1/agents":
            return {"agents": [{"id": AGENT, "owner": self.owner, "owner_dm_room": "!dm:example.org"}]}
        if path == "/v1/room" and (payload or {}).get("op") == "members":
            room = payload["room_id"]
            if room in self.rosters:
                return {"members": [{"user_id": u} for u in self.rosters[room]]}
            return {"error": "members read failed (HTTP 403)"}
        return {"ok": True}


class LoaderVoiceRoomTests(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="voice-room-loader-ws-")
        self.cfg = tempfile.mkdtemp(prefix="voice-room-loader-cfg-")
        (Path(self.ws) / "results").mkdir()
        (Path(self.ws) / "state").mkdir()
        (Path(self.ws) / "state" / "last-owner-activity.json").write_text(
            json.dumps({"ts": 4102444800, "channel": "ag2space", "summary": "t"}))
        self.ctx = _load({"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": self.ws,
                          "CLAUDE_CONFIG_DIR": self.cfg, "AG2_DEVICE_ENV": "",
                          "REMOTE_TASK_URL": "http://127.0.0.1:9", "REMOTE_TASK_TOKEN": "t",
                          "REMOTE_PROACTIVE_ROOM": "", "AGENT_MXID": AGENT},
                         f"rgb_voice_room_{id(self)}")
        self.mod = self.ctx.__enter__()
        self.gw = FakeGateway()
        self.mod._req = self.gw.req
        self.logs = []
        self.mod._log = self.logs.append

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def _result(self, name, body):
        p = Path(self.ws) / "results" / name
        p.write_text(body, encoding="utf-8")
        return p

    def test_verifier_is_wired_to_the_workspace_check_dir(self):
        self.assertEqual(self.mod.VOICE_ROOM_VERIFIER.check_dir.resolve(),
                         (Path(self.ws) / "state" / self.mod.CHECK_DIR_NAME).resolve())
        self.assertIs(self.mod.PROACTIVE_CLAIM_GATE, self.mod._ag2space_proactive_claim_gate)

    def test_gateway_room_members_reads_the_members_op(self):
        self.assertEqual(self.mod._gateway_room_members(OK_ROOM), [AGENT, OWNER])
        self.assertIsNone(self.mod._gateway_room_members(FORGED_ROOM))
        self.assertEqual(self.gw.calls[0], ("POST", "/v1/room", {"op": "members", "room_id": OK_ROOM}))

    def test_owner_is_read_once_per_ttl_and_only_when_well_formed(self):
        self.mod._VOICE_OWNER.update(mxid="", at=0.0)
        self.assertEqual(self.mod._voice_room_owner(), OWNER)
        self.assertEqual(self.mod._voice_room_owner(), OWNER)
        self.assertEqual(sum(1 for c in self.gw.calls if c[1] == "/v1/agents"), 1, "cached")
        self.mod._VOICE_OWNER.update(mxid="", at=0.0)
        self.gw.owner = "not-an-mxid"
        self.assertEqual(self.mod._voice_room_owner(), "")
        self.assertEqual(self.mod._VOICE_OWNER["mxid"], "", "a bad reading is not cached")
        self.mod._req = lambda *a, **k: "garbage"
        self.assertEqual(self.mod._voice_room_owner(), "")

    def test_voice_result_room_reads_the_channel_line(self):
        room_file = self._result("proactive-result-task-1-1.to-ag2space.txt", f"[channel: {OK_ROOM}]\nbody")
        dm_file = self._result("proactive-result-task-2-2.txt", "plain owner nudge")
        skip_file = self._result("proactive-result-task-3-3.txt", "[no-send]\nnothing")
        self.assertEqual(self.mod._voice_result_room(room_file), OK_ROOM)
        self.assertIsNone(self.mod._voice_result_room(dm_file))
        self.assertIsNone(self.mod._voice_result_room(skip_file), "a skip-marked file addresses no room")
        self.assertIsNone(self.mod._voice_result_room(Path(self.ws) / "results" / "absent.txt"))

    def test_claim_gate_holds_an_unverified_room_and_logs_once(self):
        forged = self._result("proactive-result-task-4-4.to-ag2space.txt", f"[channel: {FORGED_ROOM}]\nforged")
        self.assertFalse(self.mod._ag2space_proactive_claim_gate(forged))
        self.assertFalse(self.mod._ag2space_proactive_claim_gate(forged))
        held = [line for line in self.logs if "voice-room: holding proactive-result-task-4-4" in line]
        self.assertEqual(len(held), 1, self.logs)
        self.assertIn(FORGED_ROOM, held[0])
        self.assertEqual(sum(1 for c in self.gw.calls if c[1] == "/v1/room"), 1, "the refusal is cached")

    def test_claim_gate_passes_a_verified_room_and_a_dm_shape(self):
        ok = self._result("proactive-result-task-5-5.to-ag2space.txt", f"[channel: {OK_ROOM}]\nok")
        self.assertTrue(self.mod._ag2space_proactive_claim_gate(ok))
        self.assertNotIn(ok.name, self.mod._VOICE_ROOM_HELD)
        dm = self._result("proactive-result-task-6-6.txt", "owner nudge")
        self.assertTrue(self.mod._ag2space_proactive_claim_gate(dm), "no room named: nothing to verify")
        other = self._result("proactive-7.txt", f"[channel: {FORGED_ROOM}]\nnot a voice result")
        self.assertTrue(self.mod._ag2space_proactive_claim_gate(other),
                        "only the task bridge's proactive-result-* shape is a voice result")

    def test_held_file_is_released_once_the_room_verifies(self):
        late = self._result("proactive-result-task-8-8.to-ag2space.txt", "[channel: !late:example.org]\nlate")
        self.assertFalse(self.mod._ag2space_proactive_claim_gate(late))
        self.assertIn(late.name, self.mod._VOICE_ROOM_HELD)
        self.gw.rosters["!late:example.org"] = [AGENT, OWNER]
        self.mod.VOICE_ROOM_VERIFIER._cache.clear()
        self.assertTrue(self.mod._ag2space_proactive_claim_gate(late))
        self.assertNotIn(late.name, self.mod._VOICE_ROOM_HELD)


if __name__ == "__main__":
    unittest.main()
