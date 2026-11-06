"""Fixtures for the status-ack runner tests: a gateway lane's workspace, the gateway's own
stamped task-mid files, the real runner module and inbox-holder tables.

Import after hermetic_gateway.isolate_then_import, so the bridge module is already loaded.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
import task_envelope
import watcher_identity

ROOM = "!TaskRoomAbCdEf:ag2.space"
ROOT_EVT, MSG_EVT = "$ThreadRootEvt_1", "$AskMessageEvt_2"
T0 = 1_761_300_000.0
SESSION = watcher_identity.InboxHolders(True, [(4242, "session")], 0)
STANDBY = watcher_identity.InboxHolders(True, [(4343, "standby")], 0)


def runner():
    spec = importlib.util.spec_from_file_location(
        "status_ack_under_test", REPO / "skills" / "status-ack" / "scripts" / "status_ack.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def on(mod, **deadlines):
    """Enabled settings with the manifest defaults, a deadline override per priority allowed."""
    cfg = mod.settings(enabled="1", environ={})
    return mod.Settings(enabled=True, deadlines={**cfg.deadlines, **deadlines}, poll_s=cfg.poll_s,
                        watcher_down_after_s=cfg.watcher_down_after_s)


def task(ws: Path, tid: str, thread_root=None, stamped=True, source="ag2space", room=ROOM,
         body="please look into this", msg=MSG_EVT, timestamp=True) -> Path:
    """A task file shaped like the gateway's: routing fields below `task:`, `task_layout: mid`."""
    lines = [f"id: {tid}", f"source: {source}", f"channel_id: {room}", "priority: normal"]
    lines += [f"timestamp: {_iso(T0)}"] if timestamp else []
    lines += ["task_layout: mid", f"task: {body}"]
    if thread_root is not None:
        lines.append(f"thread_root: {thread_root}")
    lines += [f"source_room_id: {room}", f"source_message_id: {msg}"]
    text = "\n".join(lines) + "\n"
    if stamped:
        text = task_envelope.stamp_text(text, ws)
    p = ws / "tasks" / f"{tid}.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def proactive_files(ws: Path) -> list:
    return sorted(p.name for p in (ws / "results").glob("proactive-*.txt"))


def activity(ws: Path, tid: str, *phases: str) -> None:
    """Drive the task's activity card through the real activity bus, projecting nowhere."""
    import activity_bus
    store = activity_bus.ActivityStore(ws, project=lambda row: None)
    for i, phase in enumerate(phases):
        store.apply(activity_bus.LifecycleTransition(tid, phase, ts=T0 + 1 + i, room=ROOM, message_event_id=MSG_EVT))


def room_posts(lane) -> list:
    return [p for p in lane.posts if p.get("room_id") == ROOM]
