#!/usr/bin/env python3
"""A proactive result carrying `[thread: $root]` is posted inside that thread.

The send uses the same `thread_root` op:message field `room_ops.py say
--thread-root` sends; the gateway builds the m.thread relation from it.

  a) [channel: room] + [thread: $root] -> thread_root sent, room kept, body clean
  b) [thread: $root] alone             -> owner DM, thread_root sent
  c) no marker                         -> no thread_root key at all
  d) malformed [thread: x]             -> top level, logged, marker never posted
  e) [dm-only] + [channel:] + [thread:] -> owner DM, top level (the root is the room's)
  f) voice shape [channel: origin] + [thread:] + [channel: X] -> origin, top level

Imports the vendored module with an isolated env; never execs the wrapper.
Run: python3 tests/gateway-proactive-thread.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
_ISO = Path(tempfile.mkdtemp(prefix="gw-proactive-thread-"))
for _k in ("task", "result", "state"):
    (_ISO / _k).mkdir()
os.environ.update({
    "HOME": str(_ISO / "home"),
    "CLAUDE_CONFIG_DIR": str(_ISO / "claude"),
    "REMOTE_TASK_TOKEN": "https://gw.example/relay|test-token",
    "REMOTE_PROACTIVE_ROOM": "",
    "AGENT_CONNECT_TASK_DIR": str(_ISO / "task"),
    "AGENT_CONNECT_RESULT_DIR": str(_ISO / "result"),
    "AGENT_CONNECT_STATE_DIR": str(_ISO / "state"),
})

from ag2_sparrow import remote_gateway_bridge as gb  # noqa: E402

ROOM = "!TargetRoomAbCdEf:ag2.space"
OWNER_DM = "!OwnerDmRoomXyZ:ag2.space"
ROOT = "$ThreadRootEvent_123"
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def drain(body: str):
    """One _post_proactive pass over one file; returns (sent payloads, log lines)."""
    tmp = Path(tempfile.mkdtemp(dir=_ISO, prefix="drain-"))
    (tmp / "archive").mkdir()
    (tmp / "proactive-1.txt").write_text(body, encoding="utf-8")
    posts: list[dict] = []
    logs: list[str] = []

    def _fake_req(method, path, payload=None, timeout=None):
        if method == "POST":
            posts.append(payload)
        return {"ok": True, "event_id": "$evt"}

    saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
             gb._req, gb.PROACTIVE_CLAIM_GATE, gb._log)
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = tmp, tmp / "archive"
    gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE = OWNER_DM, _fake_req, None
    gb._log = logs.append
    gb._ROUTING.update(owner_dm=OWNER_DM, loaded=True, next=gb.time.time() + 3600)
    try:
        gb._post_proactive()
    finally:
        (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
         gb._req, gb.PROACTIVE_CLAIM_GATE, gb._log) = saved
        gb._ROUTING.update(owner_dm="", loaded=False, next=0.0)
    return posts, logs


def main() -> int:
    posts, _ = drain(f"[channel: {ROOM}]\n[thread: {ROOT}]\nstill on it\n")
    p = posts[0] if posts else {}
    check(len(posts) == 1, f"a) one post, got {len(posts)}")
    check(p.get("thread_root") == ROOT, f"a) thread_root sent, got {p.get('thread_root')!r}")
    check(p.get("room_id") == ROOM, f"a) the named room kept, got {p.get('room_id')!r}")
    check(p.get("body") == "still on it", f"a) body clean, got {p.get('body')!r}")

    posts, _ = drain(f"[thread: {ROOT}]\nowner update\n")
    p = posts[0] if posts else {}
    check(p.get("room_id") == OWNER_DM, f"b) owner DM, got {p.get('room_id')!r}")
    check(p.get("thread_root") == ROOT, f"b) thread_root sent, got {p.get('thread_root')!r}")

    posts, _ = drain(f"[channel: {ROOM}]\nnew topic\n")
    p = posts[0] if posts else {}
    check(p and "thread_root" not in p, f"c) no thread_root key without the marker, got {p}")
    check(p.get("body") == "new topic", f"c) body unchanged, got {p.get('body')!r}")

    posts, logs = drain(f"[channel: {ROOM}]\n[thread: not-an-event]\nupdate\n")
    p = posts[0] if posts else {}
    check(p and "thread_root" not in p, f"d) malformed -> top level, got {p}")
    check(p.get("room_id") == ROOM, f"d) still the named room, got {p.get('room_id')!r}")
    check("thread" not in (p.get("body") or ""), f"d) marker never posted, got {p.get('body')!r}")
    check(any("malformed [thread:" in line for line in logs), f"d) malformed marker logged, got {logs}")

    posts, _ = drain(f"[dm-only]\n[channel: {ROOM}]\n[thread: {ROOT}]\nprivate\n")
    p = posts[0] if posts else {}
    check(p.get("room_id") == OWNER_DM, f"e) dm-only keeps the owner DM, got {p.get('room_id')!r}")
    check(p.get("body") == "private", f"e) body clean, got {p.get('body')!r}")
    check(p and "thread_root" not in p, f"e) the room's root never reaches the DM, got {p}")

    other = "!VoiceOriginRoom:ag2.space"
    posts, logs = drain(f"[channel: {other}]\n[thread: {ROOT}]\n[channel: {ROOM}]\nupdate\n")
    p = posts[0] if posts else {}
    check(p.get("room_id") == other, f"f) the first [channel:] decides, got {p.get('room_id')!r}")
    check(p and "thread_root" not in p, f"f) X's root never posted in the origin, got {p}")
    check(p.get("body") == "update", f"f) body clean, got {p.get('body')!r}")
    check(any("may not be in the destination room" in line for line in logs), f"f) dropped root logged, got {logs}")

    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
