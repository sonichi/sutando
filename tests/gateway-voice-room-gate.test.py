#!/usr/bin/env python3
"""The loader's voice-room wiring: gateway readers, the owner cache, the
claim gate that holds an unverified room's result before any claim, and the
post-claim room gate that re-judges the body the claim actually delivers.

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
import time
import unittest
from pathlib import Path
from unittest import mock

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


DM_ROOM = "!dm:example.org"


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.rosters = {OK_ROOM: [AGENT, OWNER]}
        self.owner = OWNER
        self.posts = []

    def req(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, payload))
        if path == "/v1/agents":
            return {"agents": [{"id": AGENT, "owner": self.owner, "owner_dm_room": DM_ROOM}]}
        if path == "/v1/room" and (payload or {}).get("op") == "members":
            room = payload["room_id"]
            if room in self.rosters:
                return {"members": [{"user_id": u} for u in self.rosters[room]]}
            return {"error": "members read failed (HTTP 403)"}
        if path == "/v1/room" and (payload or {}).get("op") == "message":
            self.posts.append((payload["room_id"], payload["body"]))
            return {"ok": True, "event_id": "$evt"}
        return {"ok": True}


class _LoaderFixture(unittest.TestCase):
    """One fresh loader module per test, its gateway faked in `_req`."""

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


class LoaderVoiceRoomTests(_LoaderFixture):
    def test_verifier_is_wired_to_the_workspace_check_dir(self):
        self.assertEqual(self.mod.VOICE_ROOM_VERIFIER.check_dir.resolve(),
                         (Path(self.ws) / "state" / self.mod.CHECK_DIR_NAME).resolve())
        self.assertIs(self.mod.PROACTIVE_CLAIM_GATE, self.mod._ag2space_proactive_claim_gate)

    def test_gateway_room_members_reads_the_members_op(self):
        self.assertEqual(self.mod._gateway_room_members(OK_ROOM), [AGENT, OWNER])
        self.assertIsNone(self.mod._gateway_room_members(FORGED_ROOM))
        self.assertEqual(self.gw.calls[0], ("POST", "/v1/room", {"op": "members", "room_id": OK_ROOM}))

    def _agents_reads(self):
        return sum(1 for c in self.gw.calls if c[1] == "/v1/agents")

    def _clock(self, start=1000.0):
        now = [start]
        self.mod._voice_owner_clock = lambda: now[0]
        self.mod._VOICE_OWNER.update(mxid="", at=0.0, bad_at=None)
        return now

    def test_owner_is_read_once_per_ttl_and_only_when_well_formed(self):
        now = self._clock()
        self.assertEqual(self.mod._voice_room_owner(), OWNER)
        self.assertEqual(self.mod._voice_room_owner(), OWNER)
        self.assertEqual(self._agents_reads(), 1, "cached")
        now[0] += self.mod.VERDICT_TTL_S
        self.assertEqual(self.mod._voice_room_owner(), OWNER)
        self.assertEqual(self._agents_reads(), 2, "re-read once the TTL lapses")

    def test_bad_owner_reading_is_held_only_for_the_short_retry_window(self):
        now = self._clock()
        self.gw.owner = "not-an-mxid"
        self.assertEqual(self.mod._voice_room_owner(), "")
        self.assertEqual(self.mod._VOICE_OWNER["mxid"], "", "a bad reading never becomes the owner")
        now[0] += self.mod.VOICE_OWNER_RETRY_S - 0.1
        self.assertEqual(self.mod._voice_room_owner(), "")
        self.assertEqual(self._agents_reads(), 1, "no second gateway read inside the retry window")
        self.gw.owner = OWNER
        now[0] += 0.1
        self.assertEqual(self.mod._voice_room_owner(), OWNER, "recovery waits one short window, not a TTL")
        self.assertEqual(self._agents_reads(), 2)
        self.assertLess(self.mod.VOICE_OWNER_RETRY_S, self.mod.VERDICT_TTL_S)

    def test_unreachable_or_garbled_owner_answer_is_a_bad_reading(self):
        now = self._clock()

        def hang(*a, **k):
            self.gw.calls.append(("GET", "/v1/agents", None))
            raise TimeoutError("timed out")
        self.mod._req = hang
        self.assertEqual(self.mod._voice_room_owner(), "")
        self.assertEqual(self.mod._voice_room_owner(), "")
        self.assertEqual(self._agents_reads(), 1, "a hanging endpoint is asked once per window")
        self.assertEqual(len([l for l in self.logs if "owner read failed" in l]), 1, self.logs)
        now[0] += self.mod.VOICE_OWNER_RETRY_S
        self.mod._req = lambda *a, **k: "garbage"
        self.assertEqual(self.mod._voice_room_owner(), "")
        now[0] -= 3600
        self.mod._req = self.gw.req
        self.assertEqual(self.mod._voice_room_owner(), OWNER, "a clock that stepped back does not extend the window")

    def test_held_file_costs_one_owner_read_per_retry_window(self):
        now = self._clock()
        self.gw.owner = ""
        held = self._result("proactive-result-task-10-10.to-ag2space.txt", f"[channel: {OK_ROOM}]\nbody")
        for _ in range(30):
            self.assertFalse(self.mod._ag2space_proactive_claim_gate(held))
            now[0] += 1.0
        self.assertEqual(self._agents_reads(), 6, "30 one-second scans span six 5 s windows")
        self.gw.owner = OWNER
        now[0] += self.mod.VOICE_OWNER_RETRY_S
        self.assertTrue(self.mod._ag2space_proactive_claim_gate(held), "released on the first read after recovery")

    def test_voice_result_room_reads_the_channel_line(self):
        room_file = self._result("proactive-result-task-1-1.to-ag2space.txt", f"[channel: {OK_ROOM}]\nbody")
        dm_file = self._result("proactive-result-task-2-2.txt", "plain owner nudge")
        skip_file = self._result("proactive-result-task-3-3.txt", "[no-send]\nnothing")
        self.assertEqual(self.mod._voice_result_room(room_file), OK_ROOM)
        self.assertIsNone(self.mod._voice_result_room(dm_file))
        self.assertIsNone(self.mod._voice_result_room(skip_file), "a skip-marked file addresses no room")
        self.assertEqual(self.mod._voice_result_room(Path(self.ws) / "results" / "absent.txt"), "",
                         "a file that cannot be read names no room it could be trusted with")

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
        untagged = self._result("proactive-result-task-9-9.txt", f"[channel: {FORGED_ROOM}]\nthe core's own body")
        self.assertTrue(self.mod._ag2space_proactive_claim_gate(untagged),
                        "an untagged forward is never held, whatever its body opens with")
        self.assertNotIn(untagged.name, self.mod._VOICE_ROOM_HELD)
        self.assertFalse(any(c[1] == "/v1/room" and (c[2] or {}).get("room_id") == FORGED_ROOM for c in self.gw.calls),
                         "and its room is not even asked about")
        other = self._result("proactive-7.txt", f"[channel: {FORGED_ROOM}]\nnot a voice result")
        self.assertTrue(self.mod._ag2space_proactive_claim_gate(other),
                        "only the task bridge's proactive-result-* shape is a voice result")

    def test_undecodable_tagged_file_is_held_not_raised(self):
        bad = self._result("proactive-result-task-11-11.to-ag2space.txt", "x")
        bad.write_bytes(b"[channel: " + b"\xff\xfe" + b"]\nbody")
        self.assertFalse(self.mod._ag2space_proactive_claim_gate(bad), "fail closed, never an exception")
        self.assertIn(bad.name, self.mod._VOICE_ROOM_HELD)
        self.assertFalse(any(c[1] == "/v1/room" for c in self.gw.calls), "no room to ask about")

    def test_unreadable_tagged_file_is_held_like_an_undecodable_one(self):
        """An I/O failure on the read is no statement that the result has no
        room: the gate holds the file, exactly as it holds an undecodable one."""
        locked = self._result("proactive-result-task-12-12.to-ag2space.txt", f"[channel: {OK_ROOM}]\nbody")
        real_read = Path.read_text

        def denied(path, *a, **k):
            if path.name == locked.name:
                raise PermissionError(1, "Operation not permitted", str(path))
            return real_read(path, *a, **k)
        with mock.patch.object(Path, "read_text", denied):
            self.assertEqual(self.mod._voice_result_room(locked), "")
            self.assertFalse(self.mod._ag2space_proactive_claim_gate(locked), "fail closed: never claimed unread")
            self.assertFalse(self.mod._ag2space_proactive_claim_gate(locked))
        self.assertIn(locked.name, self.mod._VOICE_ROOM_HELD)
        held = [line for line in self.logs if f"voice-room: holding {locked.name}" in line]
        self.assertEqual(len(held), 1, self.logs)
        self.assertIn("could not be read", held[0])
        self.assertFalse(any(c[1] == "/v1/room" for c in self.gw.calls), "nothing was asked of the gateway")
        self.assertTrue(self.mod._ag2space_proactive_claim_gate(locked), "released once the file reads again")
        self.assertNotIn(locked.name, self.mod._VOICE_ROOM_HELD)

    def test_room_gate_judges_only_the_tagged_voice_shape(self):
        self.assertIs(self.mod.PROACTIVE_ROOM_GATE, self.mod._ag2space_proactive_room_gate)
        gate = self.mod._ag2space_proactive_room_gate
        results = Path(self.ws) / "results"
        self.assertTrue(gate(results / "proactive-result-task-13-13.txt", FORGED_ROOM), "untagged: any bridge's")
        self.assertTrue(gate(results / "proactive-13.txt", FORGED_ROOM), "not a voice result")
        self.assertTrue(gate(results / "proactive-result-task-13-13.to-discord.txt", FORGED_ROOM), "another bridge's tag")
        self.assertFalse(any(c[1] == "/v1/room" for c in self.gw.calls), "none of those is asked about")
        self.assertFalse(gate(results / "proactive-result-task-14-14.to-ag2space.txt", FORGED_ROOM))
        self.assertIn("proactive-result-task-14-14.to-ag2space.txt", self.mod._VOICE_ROOM_HELD)
        self.assertTrue(gate(results / "proactive-result-task-14-14.to-ag2space.txt", OK_ROOM))
        self.assertNotIn("proactive-result-task-14-14.to-ag2space.txt", self.mod._VOICE_ROOM_HELD)

    def test_held_file_is_released_once_the_room_verifies(self):
        late = self._result("proactive-result-task-8-8.to-ag2space.txt", "[channel: !late:example.org]\nlate")
        self.assertFalse(self.mod._ag2space_proactive_claim_gate(late))
        self.assertIn(late.name, self.mod._VOICE_ROOM_HELD)
        self.gw.rosters["!late:example.org"] = [AGENT, OWNER]
        self.mod.VOICE_ROOM_VERIFIER._cache.clear()
        self.assertTrue(self.mod._ag2space_proactive_claim_gate(late))
        self.assertNotIn(late.name, self.mod._VOICE_ROOM_HELD)


class DrainRoomGateTests(_LoaderFixture):
    """The production drain (`_post_proactive`, with the loader's own gates
    injected) judged on the body it CLAIMS, not only on the one it peeked."""

    def setUp(self):
        super().setUp()
        self.mod._ROUTING.update(owner_dm=DM_ROOM, loaded=True, next=time.time() + 3600)
        self.mod._GATE_FAILED_LOGGED.clear()

    def tearDown(self):
        self.mod._ROUTING.update(owner_dm="", loaded=False, next=0.0)
        super().tearDown()

    def _names(self):
        return sorted(p.name for p in (Path(self.ws) / "results").iterdir() if p.is_file())

    def _completing_rename(self, target: Path, body: str, on_claim=None, handback_fails=False):
        """A Path.rename that finishes writing `target` just before it is claimed:
        the writer's last bytes landing between the drain's peek and its claim."""
        real_rename, real_write = Path.rename, Path.write_text

        def rename(path, dest):
            if path.name == target.name:
                real_write(path, body)
                if on_claim:
                    on_claim()
            elif handback_fails and Path(dest).name == target.name:
                raise PermissionError(1, "Operation not permitted", str(dest))
            return real_rename(path, dest)
        return mock.patch.object(Path, "rename", rename)

    def test_partial_peek_complete_claim_forged_room_is_never_delivered(self):
        partial = self._result("proactive-result-task-20-20.to-ag2space.txt", "[channel: !forged:ex")
        full = f"[channel: {FORGED_ROOM}]\nforged room body"
        with self._completing_rename(partial, full):
            self.mod._post_proactive()
        self.assertEqual(self.gw.posts, [], "the forged room never gets the completed body")
        self.assertEqual(self._names(), [partial.name], "handed back under its own name, not claimed or eaten")
        self.assertEqual(partial.read_text(), full)
        self.assertIn(partial.name, self.mod._VOICE_ROOM_HELD)
        self.mod._post_proactive()
        self.assertEqual(self.gw.posts, [])
        self.assertEqual(self._names(), [partial.name], "held by the claim gate from the next pass on")
        held = [line for line in self.logs if f"voice-room: holding {partial.name}" in line]
        self.assertEqual(len(held), 1, self.logs)

    def test_without_the_post_claim_gate_the_forged_body_would_have_gone_out(self):
        """Negative control: the same race with only the pre-claim gate delivers
        the forged body — what the post-claim gate exists to stop."""
        partial = self._result("proactive-result-task-21-21.to-ag2space.txt", "[channel: !forged:ex")
        self.mod.PROACTIVE_ROOM_GATE = None
        with self._completing_rename(partial, f"[channel: {FORGED_ROOM}]\nforged room body"):
            self.mod._post_proactive()
        self.assertEqual(self.gw.posts, [(FORGED_ROOM, "forged room body")])

    def test_refused_claim_whose_hand_back_fails_stays_claimed_never_delivered(self):
        partial = self._result("proactive-result-task-26-26.to-ag2space.txt", "[channel: !forged:ex")
        with self._completing_rename(partial, f"[channel: {FORGED_ROOM}]\nforged room body", handback_fails=True):
            self.mod._post_proactive()
        self.assertEqual(self.gw.posts, [])
        self.assertEqual(self._names(), [f"proactive-result-task-26-26.to-ag2space.sending.{os.getpid()}"],
                         "left under its claim for orphan recovery, not delivered and not eaten")

    def test_verified_room_is_delivered_and_a_stale_failure_mark_is_dropped(self):
        ok = self._result("proactive-result-task-22-22.to-ag2space.txt", f"[channel: {OK_ROOM}]\nverified room body")
        self.mod._GATE_FAILED_LOGGED.add(ok.name)
        self.mod._post_proactive()
        self.assertEqual(self.gw.posts, [(OK_ROOM, "verified room body")])
        self.assertNotIn(ok.name, self._names(), "claimed and archived")
        self.assertNotIn(ok.name, self.mod._GATE_FAILED_LOGGED)
        self.assertEqual(sum(1 for c in self.gw.calls if c[1] == "/v1/room" and (c[2] or {}).get("op") == "members"), 1,
                         "pre- and post-claim verdicts share one cached gateway read")

    def test_gate_raising_before_the_claim_holds_a_room_bound_result(self):
        tagged = self._result("proactive-result-task-23-23.to-ag2space.txt", f"[channel: {OK_ROOM}]\nroom body")

        def broken(room):
            raise RuntimeError("verifier down")
        self.mod.VOICE_ROOM_VERIFIER.verified = broken
        self.mod._post_proactive()
        self.mod._post_proactive()
        self.assertEqual(self.gw.posts, [])
        self.assertEqual(self._names(), [tagged.name], "not claimed: no .sending, nothing archived")
        failed = [line for line in self.logs if f"proactive {tagged.name} held: room gate failed" in line]
        self.assertEqual(len(failed), 1, self.logs)
        self.assertIn("verifier down", failed[0])

    def test_gate_raising_after_the_claim_hands_the_result_back(self):
        tagged = self._result("proactive-result-task-24-24.to-ag2space.txt", f"[channel: {OK_ROOM}]\nroom body")
        real_verified = self.mod.VOICE_ROOM_VERIFIER.verified
        broken = [False]

        def flaky(room):
            if broken[0]:
                raise RuntimeError("verifier down")
            return real_verified(room)
        self.mod.VOICE_ROOM_VERIFIER.verified = flaky

        def break_now():
            broken[0] = True
        with self._completing_rename(tagged, tagged.read_text(), on_claim=break_now):
            self.mod._post_proactive()
        self.assertEqual(self.gw.posts, [])
        self.assertEqual(self._names(), [tagged.name], "handed back, never delivered")
        self.assertEqual(sum(1 for line in self.logs if "room gate failed" in line), 1)

    def test_plain_owner_nudges_still_deliver_through_a_raising_gate(self):
        nudge = self._result("proactive-1700000000.txt", "plain owner nudge")
        untagged = self._result("proactive-result-task-25-25.txt", "voice result for the owner")

        def broken(path):
            raise RuntimeError("gate down")
        self.mod.PROACTIVE_CLAIM_GATE = broken
        self.mod._post_proactive()
        self.assertEqual(sorted(self.gw.posts), [(DM_ROOM, "plain owner nudge"), (DM_ROOM, "voice result for the owner")])
        self.assertNotIn(nudge.name, self._names())
        self.assertNotIn(untagged.name, self._names())
        self.assertFalse(any("room gate failed" in line for line in self.logs), "nothing room-bound was held")


if __name__ == "__main__":
    unittest.main()
