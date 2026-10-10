#!/usr/bin/env python3
"""A task result carrying `[thread: $root]` is posted inside that thread.

The result server only cites the asking message, so a threaded result is handed
to the proactive leg (the same `thread_root` op:message send) and the task's
lease closes with no_send, the shape the owner-mention leg already uses.

  a) [thread: $root]                       -> one thread post in the task's room, lease closed silently
  b) no marker                             -> ordinary result, no thread post
  c) [channel: task room] + [thread:]      -> threaded in the task's room
  d) [channel: other room] + [thread:]     -> root dropped (not the task's room), ordinary redirect
  e) malformed [thread: x]                 -> ordinary result, marker never posted
  f) [dm-only] + [thread:]                 -> ordinary result, no thread post
  g) lease close fails once                -> retried, still exactly one thread post
  h) task room unknown                     -> ordinary result
  i) [thread:] + [file:]                   -> ordinary result (uploads ride the result path)
  j) a Signal (task-media) task            -> ordinary result (the server answers in its request thread)
  k) [thread:] with an empty body          -> never handed to the proactive leg (it would loop on empty)

Loads src/remote-gateway-bridge.py in-process, so the production claim gate is
installed (asserted), with Discord configured, alive and last-active; isolated
workspace, fake `_req`; never runs the wrapper as a process.
Run: python3 tests/gateway-task-result-thread.test.py
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
OWNER = "@o:example.org"
OWNER_DM = "!dm:example.org"
ROOM = "!taskroom:example.org"
OTHER = "!other:example.org"
ROOT = "$root:example.org"
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def load(ws: Path, cfg: Path):
    os.environ.update({"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws),
                       "CLAUDE_CONFIG_DIR": str(cfg), "AG2_DEVICE_ENV": "",
                       "REMOTE_TASK_URL": "http://127.0.0.1:9", "REMOTE_TASK_TOKEN": "t",
                       "REMOTE_PROACTIVE_ROOM": "", "AGENT_MXID": AGENT,
                       "DO_NOT_TRACK": "1", "SUTANDO_TELEMETRY": "0"})
    os.environ.pop("GATEWAY_INSTANCE", None)
    name = f"rgb_task_thread_{time.monotonic_ns()}"
    spec = importlib.util.spec_from_file_location(name, _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run(result: str, *, lease_fails: int = 0, forget_room: bool = False,
        attach: bool = False, signal: bool = False):
    """Write one task + result, drive the result drain and the proactive drain.
    Returns (lease posts, thread/room posts, gate installed, logs, files left)."""
    ws = Path(tempfile.mkdtemp(prefix="gw-task-thread-ws-"))
    cfg = Path(tempfile.mkdtemp(prefix="gw-task-thread-cfg-"))
    for d in ("results", "state", "logs", "tasks"):
        (ws / d).mkdir()
    (ws / "state" / "last-owner-activity.json").write_text(json.dumps(
        {"ts": int(time.time()), "channel": "discord", "summary": "t"}))
    (cfg / "channels" / "discord").mkdir(parents=True)
    (cfg / "channels" / "discord" / ".env").write_text("DISCORD_BOT_TOKEN=x\n")
    (ws / "state" / "discord-bridge.heartbeat").write_text("alive")
    mod = load(ws, cfg)
    posts: list[dict] = []
    leases: list[tuple] = []
    logs: list[str] = []
    fails = [lease_fails]

    def fake_req(method, path, payload=None, timeout=None):
        if path == "/v1/agents":
            return {"agents": [{"id": AGENT, "owner": OWNER, "owner_dm_room": OWNER_DM}]}
        if path == "/v1/room" and (payload or {}).get("op") == "message":
            posts.append(payload)
        return {"ok": True, "event_id": "$evt"}

    def fake_deliver(tid, broker_tid, body, no_send=False, result_file=None, **_kw):
        leases.append((body, no_send))
        if fails[0]:
            fails[0] -= 1
            return False
        return True

    mod._req = fake_req
    mod._log = logs.append
    mod._reenroll_identity = lambda: AGENT
    mod.LOCAL_TIER = "owner"
    mod._load_tier_map = lambda: {}
    mod._fleet_agent_ids = lambda: set()
    mod._deliver_result_payload = fake_deliver
    mod._save_inflight = lambda s: True
    mod._ROUTING.update(owner_dm=OWNER_DM, loaded=True, next=time.time() + 3600)
    written = mod._write_task({
        "id": "tt1", "task": "what is the status?", "source": "ag2space",
        "channel_id": ROOM, "source_room_id": ROOM, "source_message_id": "$ask",
        "user_id": OWNER, "access_tier": "owner"})
    tid = written[0]
    if forget_room:
        mod._save_task_rooms({})
    if signal:
        check(mod._record_task_media(tid, {"signal": {}, "thread_root": ""}), "j) media mode recorded")
    if attach:
        f = ws / "results" / "chart.png"
        f.write_bytes(b"\x89PNG\r\n")
        result = f"{result}\n[file: {f}]"
    (mod.RESULTS_DIR / f"{tid}.txt").write_text(result, encoding="utf-8")
    gate = mod.PROACTIVE_CLAIM_GATE is mod._ag2space_proactive_claim_gate
    inflight = {tid}
    for _ in range(2):
        mod._post_ready_results(inflight)
        mod._post_proactive()
    left = sorted(p.name for p in mod.RESULTS_DIR.glob("*.txt"))
    return leases, posts, gate, logs, left


def main() -> int:
    leases, posts, gate, logs, left = run(f"[thread: {ROOT}]\nall green\n")
    check(gate, "the loader's production claim gate is installed")
    p = posts[0] if posts else {}
    check(len(posts) == 1, f"a) exactly one thread post over two passes, got {len(posts)}")
    check(p.get("room_id") == ROOM, f"a) the task's room, got {p.get('room_id')!r}")
    check(p.get("thread_root") == ROOT, f"a) inside the named thread, got {p.get('thread_root')!r}")
    check(p.get("body") == "all green", f"a) body clean, got {p.get('body')!r}")
    check(leases == [("[no-send]", True)], f"a) lease closed once, silently, got {leases}")
    check(left == [], f"a) nothing left queued, got {left}")

    leases, posts, _, _, _ = run("all green\n")
    check(posts == [], f"b) no marker: no room post, got {posts}")
    check(leases == [("all green", False)], f"b) ordinary result, got {leases}")

    leases, posts, _, _, _ = run(f"[channel: {ROOM}]\n[thread: {ROOT}]\nall green\n")
    p = posts[0] if posts else {}
    check(len(posts) == 1 and p.get("room_id") == ROOM and p.get("thread_root") == ROOT
          and p.get("body") == "all green", f"c) task room named: threaded, got {posts}")
    check(leases == [("[no-send]", True)], f"c) lease closed silently, got {leases}")

    leases, posts, _, logs, _ = run(f"[channel: {OTHER}]\n[thread: {ROOT}]\nall green\n")
    check(posts == [], f"d) another room: no thread post, got {posts}")
    check(leases == [(f"[channel: {OTHER}]\nall green", False)],
          f"d) ordinary redirect, marker gone, got {leases}")
    check(any("not the task's room" in ln for ln in logs), "d) the dropped root is logged")

    leases, posts, _, logs, _ = run("[thread: not-an-event]\nall green\n")
    check(posts == [] and leases == [("all green", False)],
          f"e) malformed: ordinary result, marker never posted, got {leases} {posts}")
    check(any("malformed" in ln for ln in logs), "e) the malformed marker is logged")

    leases, posts, _, _, _ = run(f"[dm-only]\n[thread: {ROOT}]\nall green\n")
    check(posts == [], f"f) dm-only: no thread post, got {posts}")
    check(len(leases) == 1 and not leases[0][1] and "[thread:" not in leases[0][0],
          f"f) ordinary result, marker gone, got {leases}")

    leases, posts, _, _, left = run(f"[thread: {ROOT}]\nall green\n", lease_fails=1)
    check(len(posts) == 1 and (posts[0] if posts else {}).get("thread_root") == ROOT,
          f"g) lease retry: still exactly one thread post, got {posts}")
    check(leases == [("[no-send]", True)] * 2, f"g) lease close retried, got {leases}")
    check(left == [], f"g) nothing left queued, got {left}")

    leases, posts, _, logs, _ = run(f"[thread: {ROOT}]\nall green\n", forget_room=True)
    check(posts == [] and leases == [("all green", False)],
          f"h) room unknown: ordinary result, got {leases} {posts}")

    leases, posts, _, logs, _ = run(f"[thread: {ROOT}]\nall green", attach=True)
    check(posts == [] and len(leases) == 1 and not leases[0][1],
          f"i) with an attachment: ordinary result, got {leases} {posts}")
    check(any("attachments" in ln for ln in logs), "i) the reason is logged")

    leases, posts, _, logs, _ = run(f"[thread: {ROOT}]\nall green\n", signal=True)
    check(posts == [] and leases == [("all green", False)],
          f"j) Signal task: ordinary result, got {leases} {posts}")
    check(any("Signal task" in ln for ln in logs), "j) the reason is logged")

    leases, posts, _, logs, left = run(f"[thread: {ROOT}]\n")
    check(posts == [] and not any("proactive-thread" in n for n in left)
          and any("empty" in ln for ln in logs), f"k) empty body: not handed off, got {left} {posts}")

    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
