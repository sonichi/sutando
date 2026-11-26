#!/usr/bin/env python3
"""A worker lane sends its worker's owner-directed note; it never claims it only to hand it back.

The lane's own-file check (`worker_lane.claim_gate`) reads the note to see which server it is for.
The drain asks it twice: before the claim, and again after it for the body it then holds. While the
claim is held the note's live name is gone, so a check that re-read that name answered "not mine",
the note was handed back, and the rename woke every bridge watching the folder: the same note was
claimed and handed back without end, and never sent.

  a) a worker lane (not the primary) drains its worker's note -> the owner DM, once; nothing is left
  b) the check is given the claimed body, never a read of the name that is gone
  c) a host check that takes the path alone still answers

Imports the vendored module with an isolated env; never execs the wrapper.
Run: python3 tests/gateway-worker-lane-claimed-note-sent.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
sys.path.insert(0, str(REPO / "src"))
_ISO = Path(tempfile.mkdtemp(prefix="gw-worker-note-"))
for _k in ("task", "result", "state"):
    (_ISO / _k).mkdir()
OWNER_DM = "!OwnerDmRoomXyZ:ag2.space"
WORKER = "0123456789abcdef0123456789abcdef"
LANE = "s-space-01234567"
os.environ.update({
    "HOME": str(_ISO / "home"),
    "CLAUDE_CONFIG_DIR": str(_ISO / "claude"),
    "REMOTE_TASK_TOKEN": "https://gw.example/relay|test-token",
    "AGENT_CONNECT_TASK_DIR": str(_ISO / "task"),
    "AGENT_CONNECT_RESULT_DIR": str(_ISO / "result"),
    "AGENT_CONNECT_STATE_DIR": str(_ISO / "state"),
})

from ag2_sparrow import remote_gateway_bridge as gb  # noqa: E402
import worker_lane as L  # noqa: E402

FAILS: list[str] = []
NOTE = f"proactive-1700000000.by-{WORKER}.txt"


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def drain(ws: Path, own_file, passes: int = 1):
    """`passes` _post_proactive passes as a lane that owns no unaddressed note but its worker's own."""
    posts: list[dict] = []

    def _fake_req(method, path, payload=None, timeout=None, **_kw):
        if method == "POST":
            posts.append(payload)
        return {"ok": True, "event_id": "$evt"}

    saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE,
             gb.PROACTIVE_OWN_FILE, gb.OWNS_UNADDRESSED, gb._log)
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = ws / "results", ws / "results" / "archive"
    gb.PROACTIVE_ROOM, gb._req, gb._log = "", _fake_req, lambda line: None
    gb.PROACTIVE_CLAIM_GATE = gb.PROACTIVE_OWN_FILE = own_file
    gb.OWNS_UNADDRESSED = lambda: False
    gb._ROUTING.update(owner_dm=OWNER_DM, loaded=True, next=gb.time.time() + 3600)
    try:
        for _ in range(passes):
            gb._post_proactive()
    finally:
        (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE,
         gb.PROACTIVE_OWN_FILE, gb.OWNS_UNADDRESSED, gb._log) = saved
        gb._ROUTING.update(owner_dm="", loaded=False, next=0.0)
    return posts


def workspace() -> Path:
    ws = Path(tempfile.mkdtemp(dir=_ISO, prefix="ws-"))
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "results" / NOTE).write_text("Joined the room and greeted it.\n", encoding="utf-8")
    return ws


def main() -> int:
    saved_lane = L.is_worker_note_lane
    L.is_worker_note_lane = lambda instance, worker, room_id="", workspace=None: instance == LANE and worker == WORKER
    try:
        # a) The real worker-lane check, which reads the note's body to route it.
        ws = workspace()
        gate = L.claim_gate(WORKER, instance=LANE, workspace=ws, route=gb._proactive_route)
        posts = drain(ws, gate, passes=3)
        check([p.get("room_id") for p in posts] == [OWNER_DM],
              f"a) the worker's note reaches the owner DM once, got {[p.get('room_id') for p in posts]}")
        left = sorted(p.name for p in (ws / "results").glob("proactive-*"))
        check(left == [], f"a) no live note or claim is left in results, got {left}")

        # b) After the claim the check is handed the body it holds.
        ws = workspace()
        seen: list = []

        def recording(path, text=None):
            seen.append((path.name, text, path.exists()))
            return True
        drain(ws, recording)
        after_claim = [s for s in seen if s[1] is not None]
        check(len(after_claim) == 1 and after_claim[0][1].startswith("Joined the room") and not after_claim[0][2],
              f"b) one check after the claim, given the claimed body while the live name is gone, got {seen}")

        # c) A host check that takes the path alone still answers.
        ws = workspace()
        posts = drain(ws, lambda path: True)
        check([p.get("room_id") for p in posts] == [OWNER_DM], "c) a path-only host check still delivers the note")
    finally:
        L.is_worker_note_lane = saved_lane
    print("\n%d failure(s)" % len(FAILS) if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
