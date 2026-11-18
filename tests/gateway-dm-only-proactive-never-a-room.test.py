#!/usr/bin/env python3
"""An owner-only proactive notice reaches the owner's DM or nobody: never a shared room.

The schedule-crons owner notice is queued by the real producer (`seed_crons._queue_owner_dm`:
a proactive file carrying `[dm-only]`) and drained by the real gateway drain
(`_post_proactive`), with a fake gateway that records every post. The room below is the
owner's last-active conversation and the configured proactive room, the two places a
careless default would send it.

  a) the cron notice as the producer writes it            -> the owner DM, once
  b) [dm-only] with a [channel: room] before, after or far below it -> the owner DM
  c) a [dm-only] message about a task asked in the room   -> the owner DM, no room thread
  d) no owner DM known yet                                -> nothing posted; the file waits

Imports the vendored module with an isolated env; never execs the wrapper.
Run: python3 tests/gateway-dm-only-proactive-never-a-room.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
sys.path.insert(0, str(REPO / "src"))
_ISO = Path(tempfile.mkdtemp(prefix="gw-dm-only-room-"))
for _k in ("task", "result", "state"):
    (_ISO / _k).mkdir()
ROOM = "!SharedTeamRoomAbCd:ag2.space"
OWNER_DM = "!OwnerDmRoomXyZ:ag2.space"
os.environ.update({
    "HOME": str(_ISO / "home"),
    "CLAUDE_CONFIG_DIR": str(_ISO / "claude"),
    "REMOTE_TASK_TOKEN": "https://gw.example/relay|test-token",
    "REMOTE_PROACTIVE_ROOM": ROOM,
    "AGENT_CONNECT_TASK_DIR": str(_ISO / "task"),
    "AGENT_CONNECT_RESULT_DIR": str(_ISO / "result"),
    "AGENT_CONNECT_STATE_DIR": str(_ISO / "state"),
})

from ag2_sparrow import remote_gateway_bridge as gb  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "seed_crons", REPO / "skills" / "schedule-crons" / "scripts" / "seed_crons.py")
seed_crons = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_crons)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def workspace() -> Path:
    """A results dir with the owner last active in the shared room."""
    ws = Path(tempfile.mkdtemp(dir=_ISO, prefix="ws-"))
    for d in ("results/archive", "state"):
        (ws / d).mkdir(parents=True)
    (ws / "state" / "last-owner-activity.json").write_text(json.dumps(
        {"ts": 1, "channel": "sutando-room", "channel_id": ROOM, "summary": "in the team room"}))
    return ws


def drain(ws: Path, owner_dm: str = OWNER_DM, about=None, envelopes=None):
    """One _post_proactive pass over `ws/results`; returns the posts the gateway received.
    `envelopes` collects what the drain hands the outbox (its routing decision)."""
    posts: list[dict] = []

    def _fake_req(method, path, payload=None, timeout=None, **_kw):
        if method == "POST":
            posts.append(payload)
        return {"ok": True, "event_id": "$evt"}

    saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.OWNER_ACTIVITY_FILE, gb.PROACTIVE_ROOM,
             gb._req, gb.PROACTIVE_CLAIM_GATE, gb._log, gb._about_task_headers,
             gb._send_proactive, gb._proactive_dispatch_veto)
    real_send = gb._send_proactive
    if envelopes is not None:
        gb._send_proactive = lambda claim, f, env, *a: (envelopes.append(env), real_send(claim, f, env, *a))[1]
        gb._proactive_dispatch_veto = lambda *a: None  # the task's own state is not under test here
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = ws / "results", ws / "results" / "archive"
    gb.OWNER_ACTIVITY_FILE = ws / "state" / "last-owner-activity.json"
    gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE = ROOM, _fake_req, None
    gb._log = lambda line: None
    if about is not None:
        gb._about_task_headers = lambda tid: about
    gb._ROUTING.update(owner_dm=owner_dm, loaded=True, next=gb.time.time() + 3600)
    try:
        gb._post_proactive()
    finally:
        (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.OWNER_ACTIVITY_FILE, gb.PROACTIVE_ROOM,
         gb._req, gb.PROACTIVE_CLAIM_GATE, gb._log, gb._about_task_headers,
         gb._send_proactive, gb._proactive_dispatch_veto) = saved
        gb._ROUTING.update(owner_dm="", loaded=False, next=0.0)
    return posts


def rooms(posts) -> list:
    return [p.get("room_id") for p in posts]


def main() -> int:
    # a) The cron notice, exactly as the producer queues it.
    ws = workspace()
    seed_crons._queue_owner_dm(ws, {"id": "legacy-restored", "episode": 1,
                                    "message": "Your schedule was restored from the legacy file."})
    queued = sorted((ws / "results").glob("proactive-*.txt"))
    check(len(queued) == 1 and queued[0].read_text().startswith("[dm-only]\n"),
          f"a) the producer queued one [dm-only] proactive file, got {[q.name for q in queued]}")
    posts = drain(ws)
    check(rooms(posts) == [OWNER_DM], f"a) delivered to the owner DM only, got {rooms(posts)}")
    check(all("[dm-only]" not in (p.get("body") or "") for p in posts), "a) the marker is never posted")

    # b) A [channel:] naming the shared room, wherever it sits, does not move it.
    for where, body in (("before", f"[channel: {ROOM}]\n[dm-only]\nprivate detail\n"),
                        ("after", f"[dm-only]\n[channel: {ROOM}]\nprivate detail\n"),
                        ("far below", f"[channel: {ROOM}]\nprivate detail\n\nmore\n[dm-only]\n")):
        ws = workspace()
        (ws / "results" / "proactive-1.txt").write_text(body, encoding="utf-8")
        posts = drain(ws)
        check(rooms(posts) == [OWNER_DM], f"b) [channel:] {where} [dm-only] -> owner DM, got {rooms(posts)}")

    # c) About a task asked in the shared room: the DM, never that room or its thread.
    ws = workspace()
    (ws / "results" / "proactive-about-task-abc123--1.txt").write_text(
        f"[dm-only]\n[channel: {ROOM}]\nprivate progress\n", encoding="utf-8")
    envs: list[dict] = []
    posts = drain(ws, about={"source_room_id": ROOM, "source_message_id": "$askEvt"}, envelopes=envs)
    check([e.get("room_id") for e in envs] == [OWNER_DM], f"c) routed to the owner DM, got {envs}")
    check(ROOM not in rooms(posts), f"c) never posted to the task's room, got {rooms(posts)}")
    check(all("thread_root" not in e for e in envs), f"c) no thread of the room's in the DM, got {envs}")

    # d) No owner DM known: held, not posted to the configured room or the last-active one.
    ws = workspace()
    (ws / "results" / "proactive-2.txt").write_text("[dm-only]\nprivate\n", encoding="utf-8")
    posts = drain(ws, owner_dm="")
    check(posts == [], f"d) nothing posted without an owner DM, got {rooms(posts)}")
    check((ws / "results" / "proactive-2.txt").exists(), "d) the file waits for the owner DM")

    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
