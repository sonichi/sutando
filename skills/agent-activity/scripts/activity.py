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
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from file_lock import locked_file  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

KINDS = ("processing", "thinking", "working", "notice", "done")
TEXT_MAX = 160


def log_path(workspace: Path | None = None) -> Path:
    return (workspace or resolve_workspace()) / "state" / "agent-activity.jsonl"


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
        if k in ("id", "user_id", "task", "channel_id") and k not in fields:
            fields[k] = v.strip()
    task = {"id": fields.get("id") or path.stem}
    if fields.get("user_id"):
        task["from"] = fields["user_id"]
    if fields.get("task"):
        task["text"] = fields["task"][:TEXT_MAX]
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
    with locked_file(path.with_suffix(".lock"), create_mode=0o600):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        rotate(path, live_rows if live_rows is not None else LIVE_ROWS)
    return rec


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
    with open(path.with_name(path.stem + ".archive.jsonl"), "a", encoding="utf-8") as arch:
        arch.write("\n".join(rows[:-keep]) + "\n")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(rows[-keep:]) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("append", "one event"), ("done", "the task is finished: its rows leave the drawer")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("line")
        s.add_argument("--room", default=None, help="room id; default: the owner's latest AG2 Space room")
        s.add_argument("--workspace", default=None, help=argparse.SUPPRESS)  # tests only
        s.add_argument("--task-id", default=None)
        s.add_argument("--task-file", default=None, help="a task file: fills --task-id/--from/--text/--room from its headers")
        s.add_argument("--from", dest="task_from", default=None, help="mxid of the user whose message it is")
        s.add_argument("--text", dest="task_text", default=None, help="that message, trimmed to 160 chars")
        if name == "append":
            s.add_argument("--kind", choices=KINDS[:-1], default="notice")
    a = ap.parse_args(argv)
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
    done = a.cmd == "done"
    ws = Path(a.workspace) if a.workspace else None
    rec = append(a.line, kind="done" if done else a.kind, room=room or default_room(ws), task=task, done=done,
                 workspace=ws)
    print(json.dumps(rec, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
