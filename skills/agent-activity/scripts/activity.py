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
from workspace_default import resolve_workspace  # noqa: E402,F401
from activity_rows import (  # noqa: E402,F401  (the writer lives in core; the CLI keeps these names)
    KINDS, LIVE_ROWS, TEXT_MAX, append, day_of, day_range, default_room, index_path, log_path, open_task_index,
    rotate, summaries_path, summarize,
)


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
