#!/usr/bin/env python3
"""The production claim gate honours an explicit `[channel:]` room in the body.

With another bridge last-active and alive, the loader's gate deferred EVERY
undestined proactive file to that bridge, while that bridge releases a body
naming a Matrix room as foreign: a `[channel: !room]` + `[thread: $root]`
update was claimed by nobody and stayed queued.

  a) [channel: own-server room] + [thread:], discord last-active -> one post, right room+thread
  b) bare proactive, discord last-active                         -> not claimed, left on disk
  c) bare proactive, ag2space last-active                        -> delivered (positive control)
  d) [channel: room on another homeserver], discord last-active  -> not claimed
  e) [channel: discord snowflake]                                -> not claimed
  f) the peer bridges' own body gates leave the matrix-room file alone

Loads src/remote-gateway-bridge.py in-process (its real PROACTIVE_CLAIM_GATE),
with an isolated workspace and a fake `_req`; never runs the wrapper as a process.
Run: python3 tests/gateway-proactive-body-target-claim.test.py
"""
from __future__ import annotations

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
OWNER_DM = "!dm:example.org"
ROOM = "!ok:example.org"
OTHER_SERVER_ROOM = "!elsewhere:other.example"
ROOT = "$root:example.org"
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def load(ws: Path, cfg: Path):
    env = {"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws),
           "CLAUDE_CONFIG_DIR": str(cfg), "AG2_DEVICE_ENV": "",
           "REMOTE_TASK_URL": "http://127.0.0.1:9", "REMOTE_TASK_TOKEN": "t",
           "REMOTE_PROACTIVE_ROOM": "", "AGENT_MXID": AGENT}
    os.environ.update(env)
    os.environ.pop("GATEWAY_INSTANCE", None)
    name = f"rgb_body_target_{time.monotonic_ns()}"
    spec = importlib.util.spec_from_file_location(name, _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run(body: str, last_active: str, passes: int = 2):
    """Drive the real drain `passes` times; returns (posts, file still queued)."""
    ws = Path(tempfile.mkdtemp(prefix="gw-body-target-ws-"))
    cfg = Path(tempfile.mkdtemp(prefix="gw-body-target-cfg-"))
    for d in ("results", "state", "logs"):
        (ws / d).mkdir()
    (ws / "state" / "last-owner-activity.json").write_text(json.dumps(
        {"ts": int(time.time()), "channel": last_active, "summary": "t"}))
    # Discord is installed and alive here: the gate must not treat it as gone.
    (cfg / "channels" / "discord").mkdir(parents=True)
    (cfg / "channels" / "discord" / ".env").write_text("DISCORD_BOT_TOKEN=x\n")
    (ws / "state" / "discord-bridge.heartbeat").write_text("alive")
    mod = load(ws, cfg)
    posts: list[dict] = []

    def fake_req(method, path, payload=None, timeout=None):
        if path == "/v1/agents":
            return {"agents": [{"id": AGENT, "owner": "@o:example.org",
                                "owner_dm_room": OWNER_DM}]}
        if path == "/v1/room" and (payload or {}).get("op") == "message":
            posts.append(payload)
        return {"ok": True, "event_id": "$evt"}

    mod._req = fake_req
    mod._log = lambda *_a, **_k: None
    mod._reenroll_identity = lambda: AGENT
    mod._ROUTING.update(owner_dm=OWNER_DM, loaded=True, next=time.time() + 3600)
    queued = ws / "results" / "proactive-1.txt"
    queued.write_text(body, encoding="utf-8")
    gate_is_production = mod.PROACTIVE_CLAIM_GATE is mod._ag2space_proactive_claim_gate
    for _ in range(passes):
        mod._post_proactive()
    return posts, queued.exists(), gate_is_production


def main() -> int:
    posts, queued, prod = run(f"[channel: {ROOM}]\n[thread: {ROOT}]\nstill on it\n", "discord")
    check(prod, "the loader's production claim gate is installed")
    p = posts[0] if posts else {}
    check(len(posts) == 1, f"a) delivered exactly once over two passes, got {len(posts)}")
    check(p.get("room_id") == ROOM, f"a) the named room, got {p.get('room_id')!r}")
    check(p.get("thread_root") == ROOT, f"a) inside the named thread, got {p.get('thread_root')!r}")
    check(p.get("body") == "still on it", f"a) body clean, got {p.get('body')!r}")
    check(not queued, "a) file no longer queued")

    posts, queued, _ = run("owner nudge\n", "discord")
    check(posts == [] and queued, f"b) bare file follows discord last-active: posts={posts} queued={queued}")

    posts, queued, _ = run("owner nudge\n", "ag2space")
    check(len(posts) == 1 and (posts[0] if posts else {}).get("room_id") == OWNER_DM,
          f"c) bare file with ag2space last-active reaches the owner DM, got {posts}")

    posts, queued, _ = run(f"[channel: {OTHER_SERVER_ROOM}]\nfor another lane\n", "discord")
    check(posts == [] and queued, f"d) another homeserver's room is not claimed: posts={posts} queued={queued}")

    posts, queued, _ = run("[channel: 1530802402603700415]\nfor discord\n", "ag2space")
    check(posts == [] and queued, f"e) a discord target is not claimed: posts={posts} queued={queued}")

    sys.path.insert(0, str(REPO / "src"))
    from proactive_routing import body_claimable_by, redirect_target_is_foreign  # noqa: E402
    body = f"[channel: {ROOM}]\n[thread: {ROOT}]\nstill on it\n"
    for peer in ("telegram", "slack"):
        check(not body_claimable_by(body, peer), f"f) {peer}'s body gate leaves it alone")
    check(redirect_target_is_foreign(ROOM, "discord"), "f) discord releases it as foreign")

    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
