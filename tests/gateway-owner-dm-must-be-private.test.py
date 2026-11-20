#!/usr/bin/env python3
"""An owner-private message goes only to a room whose members are the agent and its owner.

The gateway registry's `owner_dm_room` was taken on trust. A row naming a shared
room (#4507: a 6-member team room) sent every bare or `[dm-only]` proactive result
there, including the daily morning briefing and the email-triage digest (#4259).

  a) registry names a shared room       -> briefing held on disk, never posted
  b) registry names the 2-member DM      -> briefing delivered there (positive control)
  c) the adopted DM gains a third member -> reading dropped, persisted file cleared
  d) membership unreadable for a new DM  -> not adopted: held
  e) a persisted reading not marked private (pre-fix file, legacy or bound) is not loaded
  f) a withheld-result review never goes to a shared room the registry names
  g) the membership read raises (gateway unreachable) -> undecided: held
  h) a review-DM cache written before the check is not reused (shared or unreadable hint)
  i) a review-DM cache marked private is reused when the hint is unreadable (positive control)
  j) the cleared reading cannot be written -> the shared room is not re-adopted from disk
  k) an unmarked record naming a room found shared is overwritten, so no other reader trusts it
  l) an owner-mention DM queued before its room went shared is held, not posted there
  m) an owner-mention DM goes to the owner DM read at send time (positive control)
  n) a pre-fix bound record naming the real DM, unmarked -> confirmed, marked private, delivered;
     the written record is stable (no further write on the next drain); one whose room went shared is cleared

Loads src/remote-gateway-bridge.py in-process with an isolated workspace and a
fake `_req`. Run: python3 tests/gateway-owner-dm-must-be-private.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ(SUTANDO_TEST_MODE="1", AG2_DEVICE_ENV="", REMOTE_TASK_URL="http://127.0.0.1:9",
                   REMOTE_TASK_TOKEN="Zq9xWv7Yu3", REMOTE_PROACTIVE_ROOM="!team:example.org",
                   AGENT_MXID="@agent:example.org")

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src" / "remote-gateway-bridge.py"
AGENT = "@agent:example.org"
OWNER = "@owner:example.org"
DM = "!dm:example.org"
TEAM = "!team:example.org"
CREATED = "!created-dm:example.org"
BRIEFING = "[dm-only]\nGood morning. 3 meetings today; 2 emails need you.\n"
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class Gateway:
    def __init__(self, dm_room: str):
        self.dm_room = dm_room
        self.members = {DM: [AGENT, OWNER], TEAM: [AGENT, OWNER, "@chi:example.org",
                                                   "@a1:example.org", "@a2:example.org", "@a3:example.org"]}
        self.unreadable: set[str] = set()
        self.unreachable: set[str] = set()
        self.posts: list[dict] = []

    def req(self, method, path, payload=None, timeout=None, **_kw):
        op = (payload or {}).get("op")
        if path == "/v1/agents":
            return {"agents": [{"id": AGENT, "owner": OWNER, "owner_dm_room": self.dm_room}]}
        if path == "/v1/room" and op == "members":
            if payload["room_id"] in self.unreachable:
                raise OSError("gateway unreachable")
            if payload["room_id"] in self.unreadable:
                return {"error": "not supported"}
            return {"members": [{"user_id": m} for m in self.members.get(payload["room_id"], [])]}
        if path == "/v1/room" and op == "create":
            return {"ok": True, "room_id": CREATED}
        if path == "/v1/room" and op == "message":
            self.posts.append(payload)
            return {"ok": True, "event_id": f"$evt{len(self.posts)}"}
        raise AssertionError((method, path, payload))


def load(ws: Path, gw: Gateway):
    cfg = Path(tempfile.mkdtemp(prefix="gw-private-cfg-"))
    os.environ.update({"SUTANDO_WORKSPACE": str(ws), "CLAUDE_CONFIG_DIR": str(cfg)})
    os.environ.pop("GATEWAY_INSTANCE", None)
    name = f"rgb_private_dm_{time.monotonic_ns()}"
    spec = importlib.util.spec_from_file_location(name, _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod._req = gw.req
    mod._log = lambda *_a, **_k: None
    mod._reenroll_identity = lambda: AGENT
    mod.PROACTIVE_CLAIM_GATE = None
    return mod


def workspace() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="gw-private-ws-"))
    for d in ("results", "state", "logs"):
        (ws / d).mkdir()
    return ws


def drain(mod, ws: Path, name: str):
    f = ws / "results" / name
    f.write_text(BRIEFING, encoding="utf-8")
    mod._post_proactive()
    return f.exists()


def main() -> int:
    ws = workspace(); gw = Gateway(TEAM); mod = load(ws, gw)
    queued = drain(mod, ws, "proactive-morning-1.txt")
    check(not any(p["room_id"] == TEAM for p in gw.posts),
          f"a) the briefing is never posted to the shared room the registry names: {gw.posts}")
    check(queued and mod.proactive_room() == "", "a) it is held on disk; no owner DM reading")

    ws = workspace(); gw = Gateway(DM); mod = load(ws, gw)
    queued = drain(mod, ws, "proactive-morning-2.txt")
    check(not queued and [p["room_id"] for p in gw.posts] == [DM],
          f"b) a 2-member DM receives the briefing: {gw.posts}")

    gw.members[DM].append("@intruder:example.org")
    mod._ROUTING["next"] = 0.0
    queued = drain(mod, ws, "proactive-morning-3.txt")
    persisted = json.loads(mod._routing_file().read_text())
    check(queued and len(gw.posts) == 1 and persisted.get("owner_dm") == "",
          f"c) a third member drops the reading, held, persisted cleared: posts={len(gw.posts)} file={persisted}")

    ws = workspace(); gw = Gateway(DM); gw.unreadable.add(DM); mod = load(ws, gw)
    queued = drain(mod, ws, "proactive-morning-4.txt")
    check(queued and gw.posts == [], f"d) unreadable membership adopts nothing: posts={gw.posts}")

    ws = workspace(); gw = Gateway(TEAM); mod = load(ws, gw)
    (ws / "state" / "owner-routing.json").write_text(json.dumps(
        {"identity": AGENT, "gateway": mod.URL, "owner_dm": TEAM}))
    mod._ROUTING.update(loaded=False, owner_dm="", persisted="")
    mod._load_routing()
    check(mod._ROUTING["owner_dm"] == "", f"e) a persisted reading not marked private is not loaded: {mod._ROUTING['owner_dm']!r}")
    mod._routing_file().write_text(json.dumps(mod._routing_record(AGENT, mod.URL, TEAM)))
    mod._ROUTING.update(owner_dm="", persisted="")
    mod._load_routing()
    check(mod._ROUTING["owner_dm"] == "",
          f"e) a bound reading not marked private is not loaded: {mod._ROUTING['owner_dm']!r}")
    mod._routing_file().write_text(json.dumps(mod._routing_record(AGENT, mod.URL, DM, private=True)))
    mod._load_routing()
    check(mod._ROUTING["owner_dm"] == DM, "e) a bound reading marked private is loaded (positive control)")

    ws = workspace(); gw = Gateway(TEAM); mod = load(ws, gw)
    mod._gateway_owner()
    room = mod._owner_review_dm(OWNER)
    check(room != TEAM, f"f) a withheld review does not go to the shared room: {room!r}")

    ws = workspace(); gw = Gateway(DM); gw.unreachable.add(DM); mod = load(ws, gw)
    queued = drain(mod, ws, "proactive-morning-5.txt")
    check(queued and gw.posts == [] and mod._owner_dm_is_private(DM, OWNER) is None,
          f"g) a members read that raises is undecided and adopts nothing: posts={gw.posts}")

    for label, hint, unreadable in (("shared", TEAM, False), ("unreadable", DM, True)):
        ws = workspace(); gw = Gateway(hint); mod = load(ws, gw)
        if unreadable:
            gw.unreadable.add(DM)
        mod._WITHHELD_DM_CACHE.parent.mkdir(parents=True, exist_ok=True)
        mod._WITHHELD_DM_CACHE.write_text(json.dumps({"owner": OWNER, "room_id": TEAM}))
        mod._gateway_owner()
        room = mod._owner_review_dm(OWNER)
        cache = json.loads(mod._WITHHELD_DM_CACHE.read_text())
        check(room == CREATED and cache == {"owner": OWNER, "room_id": CREATED, "private": True},
              f"h) {label} hint + pre-fix cache: a fresh private room, not the cached one: {room!r} {cache}")

    ws = workspace(); gw = Gateway(DM); gw.unreadable.add(DM); mod = load(ws, gw)
    mod._WITHHELD_DM_CACHE.parent.mkdir(parents=True, exist_ok=True)
    mod._WITHHELD_DM_CACHE.write_text(json.dumps({"owner": OWNER, "room_id": "!kept:example.org", "private": True}))
    mod._gateway_owner()
    check(mod._owner_review_dm(OWNER) == "!kept:example.org", "i) a cache marked private is reused")

    ws = workspace(); gw = Gateway(DM); mod = load(ws, gw)
    drain(mod, ws, "proactive-morning-6.txt")
    gw.members[DM].append("@intruder:example.org")
    mod._ROUTING["next"] = 0.0
    mod._durable_write = lambda *_a, **_k: False
    queued = drain(mod, ws, "proactive-morning-7.txt")
    queued_again = drain(mod, ws, "proactive-morning-8.txt")
    check(queued and queued_again and len(gw.posts) == 1 and mod.proactive_room() == "",
          f"j) an unwritable cleared reading holds; the shared room is not re-read from disk: posts={len(gw.posts)}")

    ws = workspace(); gw = Gateway(TEAM); mod = load(ws, gw)
    mod._routing_file().write_text(json.dumps(mod._routing_record(AGENT, mod.URL, TEAM)))
    queued = drain(mod, ws, "proactive-morning-9.txt")
    record = json.loads(mod._routing_file().read_text())
    check(queued and gw.posts == [] and record.get("owner_dm") == "" and record.get("private") is True,
          f"k) an unmarked record naming the shared room is overwritten with no reading: {record}")

    ws = workspace(); gw = Gateway(DM); mod = load(ws, gw)
    om = "proactive-owner-mention-om1.to-ag2space.txt"
    (ws / "results" / om).write_text(f"[channel: {DM}]\nAlice asked about the deck\n", encoding="utf-8")
    gw.members[DM].append("@intruder:example.org")
    mod._post_proactive()
    check((ws / "results" / om).exists() and gw.posts == [],
          f"l) an owner-mention DM queued for a room that has since gone shared is held: posts={gw.posts}")

    ws = workspace(); gw = Gateway(DM); mod = load(ws, gw)
    om = "proactive-owner-mention-om2.to-ag2space.txt"
    (ws / "results" / om).write_text("[channel: !old-dm:example.org]\nAlice asked about the deck\n", encoding="utf-8")
    mod._post_proactive()
    check(not (ws / "results" / om).exists() and [p["room_id"] for p in gw.posts] == [DM],
          f"m) an owner-mention DM goes to the owner DM read at send time, not the room baked in: {gw.posts}")

    ws = workspace(); gw = Gateway(DM); mod = load(ws, gw)
    mod._routing_file().write_text(json.dumps(mod._routing_record(AGENT, mod.URL, DM)))
    drain(mod, ws, "proactive-morning-10.txt")
    om = "proactive-owner-mention-om3.to-ag2space.txt"
    (ws / "results" / om).write_text(f"[channel: {DM}]\nAlice asked about the deck\n", encoding="utf-8")
    mod._post_proactive()
    record = json.loads(mod._routing_file().read_text())
    rooms = [p["room_id"] for p in gw.posts]
    left = sorted(f.name for f in (ws / "results").iterdir() if f.name.startswith("proactive-"))
    check(rooms == [DM, DM] and not left,
          f"n) an unmarked record naming the real DM: both items delivered once to it: {rooms} left={left}")
    check(record.get("owner_dm") and record.get("private") is True,
          f"n) the confirmed record is rewritten marked private: {record}")
    before = (mod._routing_file().read_bytes(), mod._routing_file().stat().st_mtime_ns)
    writes: list = []
    real_write = mod._durable_write
    mod._durable_write = lambda *a, **k: writes.append(a) or real_write(*a, **k)
    mod._ROUTING["next"] = 0.0
    queued = drain(mod, ws, "proactive-morning-11.txt")
    after = (mod._routing_file().read_bytes(), mod._routing_file().stat().st_mtime_ns)
    check(not queued and len(gw.posts) == 3 and gw.posts[-1]["room_id"] == DM and not writes and after == before,
          f"n) the next drain delivers with no further write: writes={len(writes)} unchanged={after == before}")

    ws = workspace(); gw = Gateway(DM); mod = load(ws, gw)
    mod._routing_file().write_text(json.dumps(mod._routing_record(AGENT, mod.URL, DM)))
    gw.members[DM].append("@intruder:example.org")
    queued = drain(mod, ws, "proactive-morning-12.txt")
    record = json.loads(mod._routing_file().read_text())
    check(queued and gw.posts == [] and record.get("owner_dm") == "" and record.get("private") is True,
          f"n) an unmarked record whose room is found shared is cleared, never marked private: {record}")

    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
