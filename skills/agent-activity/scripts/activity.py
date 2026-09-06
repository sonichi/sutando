#!/usr/bin/env python3
"""Append one row to the agent's event log, which the desktop client streams into the room's
events drawer and the dock's Events panel.

  activity.py append "<line>" --kind processing|thinking|working|notice \
      [--task-id ID --from MXID --text "<the user's message>"] [--room ROOM_ID]
  activity.py done "<what was done>" --task-id ID [--room ROOM_ID]

A row: {"ts": epoch, "room": ROOM, "line": str, "kind": str, "task": {"id","from","text"}, "done": bool}
at <workspace>/state/agent-activity.jsonl. Rows of a task stay live in the drawer until its `done` row.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

KINDS = ("processing", "thinking", "working", "notice", "done")
TEXT_MAX = 160


def log_path(workspace: Path | None = None) -> Path:
    return (workspace or resolve_workspace()) / "state" / "agent-activity.jsonl"


def summaries_path(workspace: Path | None = None) -> Path:
    """One line per finished task: what the folded card shows after the task's rows have left the
    live log. Never rotated; ~200 bytes a task."""
    return (workspace or resolve_workspace()) / "state" / "agent-activity.summaries.jsonl"


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def index_path(workspace: Path | None = None) -> Path:
    """Per open task: {task, room, started, rows, days}. Updated on every append under the writer
    lock, so a summary is exact after any rotation; an entry leaves when its task is summarized."""
    return (workspace or resolve_workspace()) / "state" / "agent-activity.index.json"


def _load_index(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_index(path: Path, d: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def day_range(start_ts: float, end_ts: float) -> list[str]:
    """Every UTC day from start to end inclusive: the complete list of archive files that can hold
    a row of the span, never just its two ends."""
    out, t = [], start_ts
    while day_of(t) <= day_of(end_ts):
        out.append(day_of(t))
        t += 86400
    return out


def open_task_index(workspace: Path | None = None) -> dict:
    """The tasks the index still holds open: what the hook falls back to when a task's rows have
    rotated out of the live log but its result now exists."""
    return _load_index(index_path(workspace))


def default_room(workspace: Path | None = None) -> str | None:
    """The room of the owner's latest AG2 Space message; a row with no room shows only in the dock."""
    p = (workspace or resolve_workspace()) / "state" / "last-owner-activity.json"
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return d.get("channel_id") if d.get("channel") == "ag2space" else None


def task_from_file(path: Path) -> tuple[dict, str | None]:
    """({id, from, text}, room) read from a task file's headers; text is the first task: line."""
    fields: dict = {}
    for l in path.read_text(encoding="utf-8", errors="replace").splitlines():
        k, _, v = l.partition(":")
        if k in ("id", "user_id", "task", "channel_id", "source_message_id") and k not in fields:
            fields[k] = v.strip()
    task = {"id": fields.get("id") or path.stem}
    if fields.get("user_id"):
        task["from"] = fields["user_id"]
    if fields.get("task"):
        task["text"] = fields["task"][:TEXT_MAX]
    if fields.get("source_message_id"):
        task["event"] = fields["source_message_id"]  # the client mounts the card under this message
    return task, fields.get("channel_id") or None


def append(line: str, *, kind: str, room: str | None, task: dict | None = None,
           done: bool = False, workspace: Path | None = None, live_rows: int | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    rec: dict = {"ts": time.time(), "line": line.strip(), "kind": kind}
    if room:
        rec["room"] = room
    if task:
        rec["task"] = task
    if done:
        rec["done"] = True
    path = log_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    # One lock for the append AND the rotation; the log is opened only under it, so no writer holds
    # an inode that a concurrent rotation replaces.
    with open(path.with_suffix(".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if task and isinstance(task.get("id"), str):
            ip = index_path(workspace)
            idx = _load_index(ip)
            e = idx.get(task["id"]) or {"started": rec["ts"], "rows": 0, "task": task, "room": room}
            e["rows"] = int(e.get("rows", 0)) + 1
            e["started"] = min(float(e.get("started", rec["ts"])), rec["ts"])
            e["task"] = dict(e.get("task") or {}, **task)
            if room:
                e["room"] = room
            if done:
                idx.pop(task["id"], None)
                summarize(rec, e, workspace)
            else:
                idx[task["id"]] = e
            _save_index(ip, idx)
        rotate(path, live_rows if live_rows is not None else LIVE_ROWS)
    return rec


def summarize(done_rec: dict, entry: dict, workspace: Path | None = None) -> dict:
    """Append the task's durable summary from the index entry — exact whether or not its rows have
    rotated out — with `days` as every UTC day of the span, the complete archive fetch list.
    Called with the writer lock held."""
    started = float(entry.get("started", done_rec["ts"]))
    summary = {"ts": done_rec["ts"], "started": started, "rows": max(int(entry.get("rows", 1)), 1),
               "days": day_range(started, done_rec["ts"]), "line": done_rec["line"], "task": done_rec["task"]}
    if done_rec.get("room"):
        summary["room"] = done_rec["room"]
    sp = summaries_path(workspace)
    with open(sp, "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary


LIVE_ROWS = 400


def rotate(path: Path, keep: int = LIVE_ROWS) -> None:
    """The live file keeps the newest `keep` rows; older rows move to <name>.archive.jsonl, so every
    reader that re-parses the live file per event stays bounded. Called with the writer lock held."""
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(rows) <= keep:
        return
    # One archive file per UTC day of the row's own timestamp, so a reader expanding an old card
    # fetches one small immutable file, not the whole history; a torn row goes to the undated file.
    by_day: dict[str, list[str]] = {}
    for line in rows[:-keep]:
        try:
            ts = json.loads(line).get("ts")
            day = day_of(ts) if isinstance(ts, (int, float)) else None
        except (ValueError, AttributeError):
            day = None
        by_day.setdefault(day, []).append(line)
    for day, lines in by_day.items():
        name = f"{path.stem}.archive.{day}.jsonl" if day else f"{path.stem}.archive.jsonl"
        with open(path.with_name(name), "a", encoding="utf-8") as arch:
            arch.write("\n".join(lines) + "\n")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(rows[-keep:]) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def queued(task_file: Path, workspace: Path | None = None) -> int:
    """A `queued` notice for a task file that just landed: the message reached the device before any
    turn picked it up. Written only when the file names a room and a message; a cron or bookkeeping
    task has neither and must not appear in the owner's room. Never falls back to the default room."""
    try:
        task, room = task_from_file(task_file)
    except OSError:
        return 0
    # Only a task that names a room AND a message can mount under one: a room-addressed task with no
    # source_message_id would leave a queued row the client can never place.
    if not room or not task.get("text") or not task.get("event"):
        return 0
    rec = append("queued", kind="notice", room=room, task=task, workspace=workspace)
    print(json.dumps(rec, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queued", help="the message reached this device and no turn has it yet; from the task file only")
    q.add_argument("--task-file", required=True)
    q.add_argument("--workspace", default=None, help=argparse.SUPPRESS)
    for name, help_ in (("append", "one event"), ("done", "the task is finished: its rows leave the drawer")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("line")
        s.add_argument("--event", dest="task_event", default=None, help="event id of the user message this row belongs to")
        if name == "done":
            s.add_argument("--into", dest="task_into", default=None,
                           help="consolidated reply: event id of the message whose reply answered this one too")
        s.add_argument("--room", default=None, help="room id; default: the owner's latest AG2 Space room")
        s.add_argument("--workspace", default=None, help=argparse.SUPPRESS)  # tests only
        s.add_argument("--task-id", default=None)
        s.add_argument("--task-file", default=None, help="a task file: fills --task-id/--from/--text/--room from its headers")
        s.add_argument("--from", dest="task_from", default=None, help="mxid of the user whose message it is")
        s.add_argument("--text", dest="task_text", default=None, help="that message, trimmed to 160 chars")
        if name == "append":
            s.add_argument("--kind", choices=KINDS[:-1], default="notice")
    a = ap.parse_args(argv)
    if a.cmd == "queued":
        return queued(Path(a.task_file), Path(a.workspace) if a.workspace else None)
    task = None
    room = a.room
    if a.task_file:
        task, file_room = task_from_file(Path(a.task_file))
        room = room or file_room
    if a.cmd == "done" and not (a.task_id or task):
        ap.error("done needs --task-id or --task-file")
    if a.task_id:
        task = dict(task or {}, id=a.task_id)
        if a.task_from:
            task["from"] = a.task_from
        if a.task_text:
            task["text"] = a.task_text[:TEXT_MAX]
    if a.task_event:
        task = dict(task or {}, event=a.task_event)
    if getattr(a, "task_into", None):
        task = dict(task or {}, into=a.task_into)
    done = a.cmd == "done"
    ws = Path(a.workspace) if a.workspace else None
    rec = append(a.line, kind="done" if done else a.kind, room=room or default_room(ws), task=task, done=done,
                 workspace=ws)
    print(json.dumps(rec, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
