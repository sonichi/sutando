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
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

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


class Mcp:
    """A scripted Actions door: discovery, mint, then MCP answers by tool name."""

    def __init__(self, tools=None, sse=False, status=200, rpc_error=False):
        self.tools, self.sse, self.status, self.rpc_error, self.sent = tools or {}, sse, status, rpc_error, []

    def __call__(self, url, token, body=None, headers=None, method="POST"):
        self.sent.append((url, token, body, headers))
        if url.endswith("/v1/mcp/discovery"):
            return 200, {}, json.dumps({"mint_url": "https://hs.example/mint",
                                        "mcp_url": "https://mcp.example/mcp"}).encode()
        if url.endswith("/mint"):
            return 200, {}, b'{"access_token": "deleg"}'
        if "id" not in body:
            return 202, {}, b""
        if body["method"] == "initialize":
            return 200, {"mcp-session-id": "s1"}, json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {}}).encode()
        result = self.tools.get(body["params"]["name"])
        msg = {"jsonrpc": "2.0", "id": body["id"]}
        msg.update({"error": {"message": "x"}} if self.rpc_error else {"result": result})
        if self.sse:
            other = json.dumps({"jsonrpc": "2.0", "id": 999, "result": {}})
            return self.status, {"content-type": "text/event-stream"}, \
                f"event: message\ndata: {other}\ndata: {json.dumps(msg)}\n".encode()
        return self.status, {}, json.dumps(msg).encode()


def ok(out):
    return {"isError": False, "structuredContent": out}


DESCRIBED = ok({"action_revision": "r", "catalog_version": "c"})


class DoorPaths(unittest.TestCase):
    def door(self, mcp):
        return nav.Door("https://hs.example/relay", "bearer", post=mcp)

    def test_sse_answers_and_session_header(self):
        mcp = Mcp({"room.actions.describe": DESCRIBED,
                   "room.action.execute": ok({"status": "completed", "result": {"event_id": "$e"}})}, sse=True)
        self.assertEqual(self.door(mcp).execute("!dm:hs", "room.navigate", {"room_id": "!r"}, "op"),
                         {"event_id": "$e"})
        self.assertEqual(mcp.sent[-1][3]["Mcp-Session-Id"], "s1")

    def test_not_completed_is_not_rerun(self):
        mcp = Mcp({"room.actions.describe": DESCRIBED,
                   "room.action.execute": ok({"status": "approval_required"})})
        with self.assertRaises(nav.Refused) as cm:
            self.door(mcp).execute("!dm:hs", "room.navigate", {}, "op")
        self.assertEqual(cm.exception.code, "NOT_COMPLETED")

    def test_tool_error_carries_the_server_code(self):
        err = {"isError": True, "content": [{"type": "text", "text": json.dumps(
            {"code": "FORBIDDEN", "message": "a focus is sent only into the DM"})}]}
        with self.assertRaises(nav.Refused) as cm:
            self.door(Mcp({"room.actions.describe": err})).call("room.actions.describe", {})
        self.assertEqual((cm.exception.code, str(cm.exception)), ("FORBIDDEN", "a focus is sent only into the DM"))

    def test_malformed_answers_refuse(self):
        for tools, kw in (({"t": None}, {}), ({"t": {"isError": False}}, {}),
                          ({"t": ok({})}, {"status": 500}), ({"t": ok({})}, {"rpc_error": True})):
            with self.assertRaises(nav.Refused):
                self.door(Mcp(tools, **kw)).call("t", {})

    def test_discovery_and_mint_refusals(self):
        def no_mcp(url, token, body=None, headers=None, method="POST"):
            return 404, {}, b""
        with self.assertRaises(nav.Refused) as cm:
            nav.Door("https://hs.example/relay", "b", post=no_mcp)
        self.assertEqual(cm.exception.code, "NO_MCP")

        def no_mint(url, token, body=None, headers=None, method="POST"):
            if url.endswith("discovery"):
                return 200, {}, json.dumps({"mint_url": "https://hs.example/m", "mcp_url": "https://m/x"}).encode()
            return 401, {}, b"{}"
        with self.assertRaises(nav.Refused) as cm:
            nav.Door("https://hs.example/relay", "b", post=no_mint)
        self.assertEqual(cm.exception.code, "MINT")

    def test_navigate_reports_both_outcomes(self):
        self.assertEqual(nav.navigate("!dm", {"room_id": "!r"}, FakeDoor()), {"ok": True, "event_id": "$nav1"})
        out = nav.navigate("!dm", {"room_id": "!r"}, FakeDoor(fail="no"))
        self.assertEqual((out["ok"], out["code"]), (False, "FORBIDDEN"))

    def test_open_door_needs_a_gateway(self):
        with patch.object(nav._gw, "gateway", lambda: ("", {})):
            with self.assertRaises(nav.Refused):
                nav.open_door()
        with patch.object(nav._gw, "gateway", lambda: ("https://hs.example/relay", {"Authorization": "Bearer t"})), \
                patch.object(nav, "Door", lambda base, bearer: (base, bearer)):
            self.assertEqual(nav.open_door(), ("https://hs.example/relay", "t"))


class Transport(unittest.TestCase):
    """_post against a real loopback server: success, an HTTP error, an unreachable host."""

    @classmethod
    def setUpClass(cls):
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                code = 200 if self.path == "/ok" else 403
                self.send_response(code)
                self.send_header("Mcp-Session-Id", "s9")
                self.end_headers()
                self.wfile.write(body or b"{}")

            def log_message(self, *a):
                pass
        cls.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_round_trip(self):
        status, hdrs, raw = nav._post(self.base + "/ok", "t", {"a": 1})
        self.assertEqual((status, hdrs["mcp-session-id"], json.loads(raw)), (200, "s9", {"a": 1}))

    def test_http_error_is_a_status(self):
        self.assertEqual(nav._post(self.base + "/no", "t", {"a": 1})[0], 403)

    def test_unreachable_refuses(self):
        with self.assertRaises(nav.Refused) as cm:
            nav._post("http://127.0.0.1:9/x", "t")
        self.assertEqual(cm.exception.code, "UNREACHABLE")


class GatewayReads(unittest.TestCase):
    """Rooms reads through the room-ops modules; stubbed at their seams."""

    def test_reads(self):
        import members as m_mod
        import read as r_mod
        import rooms as rooms_mod
        with patch.object(nav._gw, "gateway", lambda: ("https://hs/relay", {})), \
                patch.object(nav._gw, "http_json", lambda *a, **k: (200, {"agents": [{"id": "@a"}]})), \
                patch.object(rooms_mod, "joined_rooms", lambda: {"rooms": ["!a", 3]}), \
                patch.object(m_mod, "room_members", lambda r: {"ok": r == "!a", "members": [{"user_id": "@a"}]}), \
                patch.object(r_mod, "read_room", lambda r, limit: {"messages": [
                    {"sender": "@o", "ts": 5}, {"sender": "@o", "ts": 9}, {"sender": "@x", "ts": 99}]}):
            r = nav.Rooms()
            self.assertEqual(r.agents(), [{"id": "@a"}])
            self.assertEqual(r.joined(), ["!a"])
            self.assertEqual((r.members("!a"), r.members("!b")), ({"@a"}, None))
            self.assertEqual((r.last_message_by("!a", "@o"), r.last_message_by("!a", "@n")), (9.0, None))


class EdgesAndCli(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="nav-cli-"))

    def test_no_identity_or_owner_resolves_nothing(self):
        (self.ws / "state").mkdir()
        self.assertEqual(nav.owner_dm_room(self.ws, GW, FakeRooms({}, {})), "")
        os.environ["AGENT_MXID"] = "@nobody:hs"
        try:
            self.assertEqual(nav.owner_dm_room(self.ws, GW, FakeRooms({}, {})), "")
        finally:
            os.environ.pop("AGENT_MXID")
        self.assertEqual(nav.pick_owner_dm([], lambda r: None), ("", ""))

    def test_corrupt_ledger_and_cache_read_as_empty(self):
        (self.ws / "state").mkdir()
        (self.ws / "state" / "owner-mention-navigate.json").write_text("[1]")
        (self.ws / "state" / "owner-mention-dm.json").write_text("not json")
        with nav.Ledger(self.ws) as led:
            self.assertEqual(led.data, {"seen": {}})
        self.assertEqual(nav._read_dm_cache(self.ws), {})
        (self.ws / "state" / "owner-mention-dm.json").write_text('{"agent": 1, "at": "x", "room": "!r"}')
        self.assertEqual(nav.owner_dm_room(self.ws, GW, FakeRooms({}, {})), "")

    def test_small_parsers(self):
        self.assertIsNone(nav.clip_reason("   "))
        self.assertIsNone(nav._json(b"not json"))
        self.assertEqual(nav.decide(nav.Mention(ROOM, ""), {}, 1.0, OWNER_DM, 120).why,
                         "the task attests no room or message id")

    def test_a_malformed_cache_rescans(self):
        (self.ws / "state").mkdir()
        (self.ws / "state" / "owner-routing.json").write_text(json.dumps({"identity": AGENT, "gateway": GW}))
        (self.ws / "state" / "owner-mention-dm.json").write_text(json.dumps(
            {"agent": AGENT, "gateway": GW, "at": "x", "room": "!stale:hs"}))
        rooms = FakeRooms({"!dm:hs": {AGENT, OWNER}}, {})
        self.assertEqual(nav.owner_dm_room(self.ws, GW, rooms), "!dm:hs")

    def test_resolves_the_dm_when_not_given(self):
        with patch.object(nav, "owner_dm_room", lambda ws, base: OWNER_DM), \
                patch.object(nav._gw, "gateway", lambda: (GW, {})):
            self.assertTrue(nav.on_mention(nav.Mention(ROOM, "$a"), self.ws, now=1000.0, window=120,
                                           door_factory=FakeDoor())["navigated"])
            nav.on_mention(nav.Mention(ROOM, "$b"), self.ws, now=1010.0, window=120,
                           door_factory=FakeDoor(), spawn_flush=lambda at: None)
            door = FakeDoor(fail="a focus is sent only into the DM between this agent and its owner")
            self.assertFalse(nav.flush(self.ws, now=2000.0, window=120, door_factory=door,
                                       dm_writer=lambda ws, line: None)["navigated"])
        self.assertEqual(nav._read_dm_cache(self.ws)["refused"], [OWNER_DM])

    def test_seen_is_bounded(self):
        now = 1000.0 + nav.SEEN_TTL_S
        data = {"seen": {f"$e{i}": now - i for i in range(1, nav.SEEN_MAX + 2)} | {"$old": 1.0}}
        nav._remember(data, "$new", now)
        self.assertNotIn("$old", data["seen"])
        self.assertIn("$new", data["seen"])
        self.assertLessEqual(len(data["seen"]), nav.SEEN_MAX)

    def test_window_from_env_and_a_broken_manifest(self):
        with patch.dict(os.environ, {nav.WINDOW_CONFIG_KEY: "45"}):
            self.assertEqual(nav.window_seconds(), 45.0)
        with patch.object(nav.Path, "read_text", side_effect=OSError):
            self.assertEqual(nav.window_seconds(), nav.DEFAULT_WINDOW_S)

    def test_instance_suffix(self):
        with patch.dict(os.environ, {"GATEWAY_INSTANCE": "dev"}):
            self.assertEqual(nav._instance_suffix(), ".dev")

    def test_flush_without_an_owner_dm_drops_the_hold(self):
        nav.on_mention(nav.Mention(ROOM, "$a"), self.ws, now=1000.0, owner_dm=OWNER_DM, window=120,
                       door_factory=FakeDoor())
        nav.on_mention(nav.Mention(ROOM, "$b"), self.ws, now=1010.0, owner_dm=OWNER_DM, window=120,
                       door_factory=FakeDoor(), spawn_flush=lambda at: None)
        out = nav.flush(self.ws, now=2000.0, owner_dm="", window=120, door_factory=FakeDoor())
        self.assertEqual(out["skipped"], "no owner DM reading for this gateway")

    def test_spawn_log_and_dm_line(self):
        with patch.object(nav.subprocess, "Popen") as popen:
            nav._spawn_flush(1234.5)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[2:], ["navigate", "flush", "--not-before", "1234.5"])
        self.assertTrue(argv[1].endswith("room_ops.py"))
        nav._log(self.ws, "hello")
        self.assertIn("hello", (self.ws / "logs" / "owner-mention-navigate.log").read_text())
        with patch.object(nav.Path, "mkdir", side_effect=OSError):
            nav._log(self.ws, "lost")  # a log failure never raises
        nav._write_dm_line(self.ws, "a DM line")
        files = list((self.ws / "results").glob("proactive-*.to-ag2space.txt"))
        self.assertEqual([f.read_text() for f in files], ["a DM line\n"])
        with patch.object(nav._gw, "_core_src_on_path", lambda: False):
            nav._write_dm_line(self.ws, "x")
            self.assertIsNone(nav.mention_from_task(task_text()))
            with self.assertRaises(RuntimeError):
                nav._workspace()
        import workspace_default
        with patch.object(workspace_default, "resolve_workspace", lambda: self.ws):
            self.assertEqual(nav._workspace(), self.ws)

    def cli(self, *argv):
        import argparse
        ap = argparse.ArgumentParser()
        nav.add_arguments(ap)
        return nav.run(ap.parse_args(list(argv)))

    def test_cli(self):
        task = self.ws / "t.txt"
        task.write_text(task_text(owner_mentioned=False))
        with patch.object(nav, "_workspace", lambda: self.ws), \
                patch.object(nav, "owner_dm_room", lambda ws, base: ""), \
                patch.object(nav._gw, "gateway", lambda: ("https://hs/relay", {})), \
                patch.object(nav, "navigate", lambda dm, args, *a: {"ok": True, "dm": dm, "args": args}):
            self.assertFalse(self.cli("to", ROOM)["ok"])
            out = self.cli("to", ROOM, "--owner-dm", OWNER_DM, "--event", "$e", "--reason", "r")
            self.assertEqual((out["dm"], out["args"]["location"]), (OWNER_DM, {"event_id": "$e"}))
            self.assertEqual(self.cli("mention", "--task-file", str(task))["skipped"],
                             "not an owner-mention task")
            self.assertFalse(self.cli("mention", "--task-file", str(self.ws / "missing"))["ok"])
            with patch.object(nav.time, "sleep") as slept:
                self.assertEqual(self.cli("flush", "--not-before", str(time.time() + 5))["skipped"], "nothing held")
            self.assertTrue(slept.called)

    def test_room_ops_dispatches_navigate(self):
        import io
        import room_ops
        with patch.object(nav, "run", lambda a: {"ok": True, "cmd": a.nav_cmd}) as _r, \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(room_ops._main(["navigate", "flush"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"ok": True, "cmd": "flush"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
