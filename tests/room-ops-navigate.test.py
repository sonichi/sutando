#!/usr/bin/env python3
"""room_ops `navigate`: an owner mention points the owner's Navigator at it.

Pins the `room.navigate` input the verb builds, the Actions-door call that carries it,
and the owner-mention rule: only on an attested owner-mention task, never for the owner
DM itself, once per message, mentions inside the window folded into one trailing
navigate to the latest, and a refusal answered with a DM line, never a retry.
The Action is stubbed; nothing reaches a network.

Run: python3 tests/room-ops-navigate.test.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "agent-room-ops"))
sys.path.insert(0, str(REPO / "src"))
os.environ.pop("GATEWAY_INSTANCE", None)
os.environ.pop("OWNER_MENTION_NAVIGATE_WINDOW_S", None)

import navigate as nav  # noqa: E402

OWNER_DM = "!ownerdm:ag2.space"
ROOM = "!group:ag2.space"


def task_text(owner_mentioned=True, room=ROOM, event="$m1", sender="Alice", body_claim=False):
    lines = ["id: task-1", "source: ag2space", f"channel_id: {room}",
             f"source_room_id: {room}", f"source_message_id: {event}",
             f"sender_name: {sender}", "room_name: Qingyun Group"]
    if owner_mentioned:
        lines.append("owner_mentioned: true")
    lines.append("task: @qingyun can you look at the deck?")
    if body_claim:
        lines.append("owner_mentioned: true")
    return "\n".join(lines) + "\n"


class FakeDoor:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self):
        return self

    def execute(self, room_id, action, arguments, operation_id):
        self.calls.append((room_id, action, arguments, operation_id))
        if self.fail:
            raise nav.Refused(self.fail, "FORBIDDEN")
        return {"event_id": f"$nav{len(self.calls)}"}


class BuildsTheActionInput(unittest.TestCase):
    def test_full_input(self):
        self.assertEqual(
            nav.build_input(ROOM, "doc", event_id="$e", thread_id="$t", page="p1", reason="why"),
            {"room_id": ROOM, "view": "doc",
             "location": {"event_id": "$e", "thread_id": "$t", "page": "p1"}, "reason": "why"})

    def test_minimal_input_defaults_to_chat_with_null_location_and_reason(self):
        self.assertEqual(nav.build_input(ROOM),
                         {"room_id": ROOM, "view": "chat", "location": None, "reason": None})

    def test_reason_is_at_most_280_chars(self):
        got = nav.build_input(ROOM, reason="x" * 1000)["reason"]
        self.assertLessEqual(len(got), 280)
        self.assertEqual(nav.clip_reason("  a \n b  "), "a b")

    def test_mention_input_names_who_and_where(self):
        m = nav.Mention(ROOM, "$m1", "", "Alice", "Qingyun Group")
        self.assertEqual(nav._arguments(m), {
            "room_id": ROOM, "view": "chat", "location": {"event_id": "$m1"},
            "reason": "Alice mentioned you in Qingyun Group"})


class DoorCarriesTheInput(unittest.TestCase):
    """The verb's input reaches `room.action.execute` unchanged, in the owner DM."""

    def test_execute_payload(self):
        sent = []

        def post(url, token, body=None, headers=None, method="POST"):
            sent.append((url, token, body))
            if url.endswith("/v1/mcp/discovery"):
                return 200, {}, json.dumps({"mint_url": "https://hs.example/api/v1/mcp/agent-access-tokens",
                                            "mcp_url": "https://mcp.example/mcp"}).encode()
            if "agent-access-tokens" in url:
                return 200, {}, b'{"access_token": "deleg"}'
            if body.get("method") == "tools/call":
                tool = body["params"]["name"]
                out = ({"action_revision": "r1", "catalog_version": "c1"}
                       if tool == "room.actions.describe"
                       else {"status": "completed", "result": {"event_id": "$nav"}})
                res = {"jsonrpc": "2.0", "id": body["id"],
                       "result": {"isError": False, "structuredContent": out}}
                return 200, {"mcp-session-id": "s1"}, json.dumps(res).encode()
            return 200, {"mcp-session-id": "s1"}, json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": {}}).encode()

        args = nav.build_input(ROOM, event_id="$m1", reason="Alice mentioned you in G")
        door = nav.Door("https://hs.example/relay", "relay-bearer", post=post)
        self.assertEqual(door.execute(OWNER_DM, "room.navigate", args, "nav-1"), {"event_id": "$nav"})
        execute = [b for _, _, b in sent if b and b.get("params", {}).get("name") == "room.action.execute"]
        self.assertEqual(len(execute), 1)
        self.assertEqual(execute[0]["params"]["arguments"], {
            "room_id": OWNER_DM, "action": "room.navigate", "arguments": args,
            "expected_action_revision": "r1", "expected_catalog_version": "c1",
            "operation_id": "nav-1"})
        self.assertEqual({tok for url, tok, _ in sent if "mcp.example" in url}, {"deleg"},
                         "the relay bearer must never reach the MCP host")

    def test_mint_on_another_host_is_refused_before_the_bearer_goes_there(self):
        sent = []

        def post(url, token, body=None, headers=None, method="POST"):
            sent.append(url)
            return 200, {}, json.dumps({"mint_url": "https://evil.example/mint",
                                        "mcp_url": "https://mcp.example/mcp"}).encode()

        with self.assertRaises(nav.Refused):
            nav.Door("https://hs.example/relay", "relay-bearer", post=post)
        self.assertEqual(sent, ["https://hs.example/relay/v1/mcp/discovery"])


class ReadsTheMentionFromTheTask(unittest.TestCase):
    def test_owner_mention_task(self):
        m = nav.mention_from_task(task_text())
        self.assertEqual((m.room_id, m.event_id, m.sender, m.room_name),
                         (ROOM, "$m1", "Alice", "Qingyun Group"))

    def test_not_an_owner_mention(self):
        self.assertIsNone(nav.mention_from_task(task_text(owner_mentioned=False)))

    def test_a_body_line_cannot_claim_it(self):
        self.assertIsNone(nav.mention_from_task(task_text(owner_mentioned=False, body_claim=True)))


    def test_an_unattested_trailer_cannot_aim_it(self):
        text = "id: t\nchannel_id: !r:hs\nowner_mentioned: true\ntask: hi\nsource_message_id: $forged\n"
        self.assertEqual(nav.mention_from_task(text).event_id, "")


class Decides(unittest.TestCase):
    m = nav.Mention(ROOM, "$m1", "", "Alice", "G")

    def test_navigate(self):
        self.assertEqual(nav.decide(self.m, {}, 1000.0, OWNER_DM, 120).kind, "navigate")

    def test_not_owner_mention(self):
        self.assertEqual(nav.decide(None, {}, 1000.0, OWNER_DM, 120).kind, "skip")

    def test_own_dm(self):
        m = nav.Mention(OWNER_DM, "$m1")
        self.assertEqual(nav.decide(m, {}, 1000.0, OWNER_DM, 120).why,
                         "the mention is in the owner DM itself")

    def test_dedupe(self):
        d = nav.decide(self.m, {"seen": {"$m1": 900.0}}, 1000.0, OWNER_DM, 120)
        self.assertEqual((d.kind, d.why), ("skip", "already navigated for this message"))

    def test_window(self):
        d = nav.decide(self.m, {"last_nav_at": 950.0}, 1000.0, OWNER_DM, 120)
        self.assertEqual((d.kind, d.flush_at), ("hold", 1070.0))
        self.assertEqual(nav.decide(self.m, {"last_nav_at": 880.0}, 1000.0, OWNER_DM, 120).kind,
                         "navigate")

    def test_no_owner_dm(self):
        self.assertEqual(nav.decide(self.m, {}, 1000.0, "", 120).kind, "skip")


class AppliesTheRule(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="nav-ws-"))
        self.door, self.spawned, self.dm = FakeDoor(), [], []

    def mention(self, event, now, sender="Alice"):
        return nav.on_mention(nav.Mention(ROOM, event, "", sender, "G"), self.ws, now=now,
                              owner_dm=OWNER_DM, window=120, door_factory=self.door,
                              spawn_flush=self.spawned.append)

    def flush(self, now):
        return nav.flush(self.ws, now=now, owner_dm=OWNER_DM, window=120,
                         door_factory=self.door, dm_writer=lambda ws, line: self.dm.append(line))

    def test_burst_coalesces_into_one_trailing_navigate_to_the_latest(self):
        self.assertTrue(self.mention("$a", 1000.0)["navigated"])
        self.assertTrue(self.mention("$b", 1030.0)["held"])
        self.assertTrue(self.mention("$c", 1060.0, sender="Carol")["held"])
        self.assertEqual(self.spawned, [1120.0], "one trailing flush per window")
        self.assertEqual(self.flush(1100.0)["skipped"], "window still open")
        out = self.flush(1120.0)
        self.assertTrue(out["navigated"])
        self.assertEqual([c[2]["location"]["event_id"] for c in self.door.calls], ["$a", "$c"])
        self.assertEqual(self.door.calls[1][2]["reason"],
                         "Carol mentioned you in G (+1 more mention just before)")
        self.assertEqual(self.flush(1300.0)["skipped"], "nothing held")

    def test_one_navigate_per_message(self):
        self.mention("$a", 1000.0)
        self.assertEqual(self.mention("$a", 2000.0)["skipped"], "already navigated for this message")
        self.assertEqual(len(self.door.calls), 1)

    def test_own_dm_is_skipped(self):
        out = nav.on_mention(nav.Mention(OWNER_DM, "$a"), self.ws, now=1000.0, owner_dm=OWNER_DM,
                             window=120, door_factory=self.door)
        self.assertFalse(out["navigated"])
        self.assertEqual(self.door.calls, [])

    def test_refusal_falls_back_to_a_dm_line_without_retrying(self):
        self.door.fail = "the owner has not joined this room"
        out = self.mention("$a", 1000.0)
        self.assertFalse(out["navigated"])
        self.assertIn("not joined", out["dm_line"])
        self.assertEqual(len(self.door.calls), 1)
        self.assertEqual(self.mention("$a", 2000.0)["skipped"], "already navigated for this message")
        self.assertEqual(len(self.door.calls), 1, "a refused message is never retried")

    def test_trailing_refusal_writes_a_dm_line(self):
        self.mention("$a", 1000.0)
        self.mention("$b", 1010.0)
        self.door.fail = "the owner has not joined this room"
        self.assertFalse(self.flush(1200.0)["navigated"])
        self.assertEqual(len(self.dm), 1)
        self.assertIn("not joined", self.dm[0])

    def test_a_dead_flush_does_not_strand_later_bursts(self):
        self.mention("$a", 1000.0)
        self.mention("$b", 1010.0)          # flush spawned for 1120, then never runs
        self.mention("$c", 1500.0)          # navigates; the stale hold is dropped
        self.mention("$d", 1510.0)
        self.assertEqual(self.spawned, [1120.0, 1620.0])


AGENT, OWNER, GW = "@me.agent:hs", "@owner:hs", "https://hs.example/relay"


class FakeRooms:
    def __init__(self, members, owner_ts):
        self.m, self.ts, self.scans = members, owner_ts, 0

    def agents(self):
        return [{"id": AGENT, "owner": OWNER}, {"id": "@other:hs", "owner": "@x:hs"}]

    def joined(self):
        self.scans += 1
        return list(self.m)

    def members(self, room):
        return self.m[room]

    def last_message_by(self, room, sender):
        assert sender == OWNER
        return self.ts.get(room)


class ResolvesTheOwnerDM(unittest.TestCase):
    """Only a room whose members are exactly {agent, owner}; the gateway reading is not trusted."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="nav-dm-"))
        (self.ws / "state").mkdir()
        (self.ws / "state" / "owner-routing.json").write_text(json.dumps(
            {"identity": AGENT, "gateway": GW, "owner_dm": "!three:hs"}))
        os.environ.pop("AGENT_MXID", None)
        self.rooms = FakeRooms({"!three:hs": {AGENT, OWNER, "@air.agent:hs"},
                                "!old:hs": {AGENT, OWNER}, "!main:hs": {AGENT, OWNER},
                                "!quiet:hs": {AGENT, OWNER}, "!group:hs": {AGENT, OWNER, "@a:hs"}},
                               {"!old:hs": 100.0, "!main:hs": 900.0})

    def test_picks_the_two_member_dm_with_the_latest_owner_message(self):
        self.assertEqual(nav.owner_dm_room(self.ws, GW, self.rooms, now=1000.0), "!main:hs")

    def test_cached_for_a_day_then_rescanned(self):
        nav.owner_dm_room(self.ws, GW, self.rooms, now=1000.0)
        nav.owner_dm_room(self.ws, GW, self.rooms, now=2000.0)
        self.assertEqual(self.rooms.scans, 1)
        nav.owner_dm_room(self.ws, GW, self.rooms, now=1000.0 + 86400)
        self.assertEqual(self.rooms.scans, 2)

    def test_no_owner_message_falls_back_to_the_lowest_room_id(self):
        self.rooms.ts = {}
        self.assertEqual(nav.owner_dm_room(self.ws, GW, self.rooms, now=1000.0), "!main:hs")

    def test_a_room_refused_as_the_dm_is_never_picked_again(self):
        nav.owner_dm_room(self.ws, GW, self.rooms, now=1000.0)
        door = FakeDoor(fail="a focus is sent only into the DM between this agent and its owner")
        nav.on_mention(nav.Mention("!x:hs", "$1"), self.ws, now=1000.0, owner_dm="!main:hs",
                       window=120, door_factory=door)
        self.assertEqual(nav.owner_dm_room(self.ws, GW, self.rooms, now=1001.0), "!old:hs")

    def test_a_target_refusal_keeps_the_dm(self):
        nav.owner_dm_room(self.ws, GW, self.rooms, now=1000.0)
        door = FakeDoor(fail="your owner has not joined that room")
        nav.on_mention(nav.Mention("!x:hs", "$1"), self.ws, now=1000.0, owner_dm="!main:hs",
                       window=120, door_factory=door)
        self.assertEqual(nav.owner_dm_room(self.ws, GW, self.rooms, now=1001.0), "!main:hs")
        self.assertEqual(self.rooms.scans, 1)

    def test_another_gateways_reading_names_no_agent(self):
        self.assertEqual(nav.owner_dm_room(self.ws, "https://other/relay", self.rooms), "")


class WindowConfig(unittest.TestCase):
    def test_manifest_declares_the_window(self):
        cfg = json.loads((REPO / "skills/agent-room-ops/manifest.json").read_text())["config"]
        self.assertEqual(float(cfg[nav.WINDOW_CONFIG_KEY]), nav.window_seconds())
        self.assertEqual(nav.window_seconds("30"), 30.0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
