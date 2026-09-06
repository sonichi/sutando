#!/usr/bin/env python3
"""The agent-activity row writer: one JSON row per line at <workspace>/state/agent-activity.jsonl,
the live window the desktop renders, its per-day archive, the per-task index that keeps a summary
exact after rotation, and the summary left at done.

One owner: the agent-activity skill's CLI and the activity bus both write through here, so the
lock, the rotation, the index and the summary cannot drift between them. Row shape is the contract
the client reads: {"ts", "room", "line", "kind", "task": {"id","from","text","event","into"}, "done"}.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from workspace_default import resolve_workspace

KINDS = ("processing", "thinking", "working", "notice", "done")
TEXT_MAX = 160
LIVE_ROWS = 400


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


def acks_dir(workspace: Path | None = None) -> Path:
    """Per task, the projection ids the writer has applied — the sink's acknowledgement, kept apart
    from the rotating live log and per task so unrelated traffic can never evict an owed row's ack."""
    return log_path(workspace).with_name("agent-activity.acks")


def _pid_acked(workspace: Path | None, task_id: str, pid: str) -> bool:
    try:
        return pid in (acks_dir(workspace) / task_id).read_text(encoding="utf-8").split("\n")
    except OSError:
        return False


def _ack(workspace: Path | None, task_id: str, pid: str) -> None:
    d = acks_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / task_id, "a", encoding="utf-8") as f:
        f.write(pid + "\n")


def _ack_close(workspace: Path | None, task_id: str) -> None:
    """The task is done: its summary carries the done pid, so the per-task file can go."""
    try:
        (acks_dir(workspace) / task_id).unlink()
    except OSError:
        pass


def _pid_in_log(path: Path, pid: str) -> bool:
    try:
        return any(json.loads(l).get("pid") == pid for l in path.read_text(encoding="utf-8").splitlines() if l.strip())
    except (OSError, ValueError):
        return False


def append(line: str, *, kind: str, room: str | None, task: dict | None = None,
           done: bool = False, workspace: Path | None = None, live_rows: int | None = None,
           pid: str | None = None) -> dict:
    """`pid` is the row's stable projection identity: a replay after a partial write (row appended,
    index or summary not) is applied exactly once, each half checking what already landed."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    rec: dict = {"ts": time.time(), "line": line.strip(), "kind": kind}
    if pid:
        rec["pid"] = pid
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
        task_id = task.get("id") if isinstance(task, dict) and isinstance(task.get("id"), str) else None
        acked = bool(pid and task_id and (_pid_acked(workspace, task_id, pid)
                                          or (done and _pid_in_log(summaries_path(workspace), pid))))
        if not (pid and (acked or _pid_in_log(path, pid))):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if pid and task_id:
                _ack(workspace, task_id, pid)
        if task and isinstance(task.get("id"), str):
            ip = index_path(workspace)
            idx = _load_index(ip)
            e = idx.get(task["id"]) or {"started": rec["ts"], "rows": 0, "task": task, "room": room}
            if not (pid and e.get("last_pid") == pid):
                e["rows"] = int(e.get("rows", 0)) + 1
                e["last_pid"] = pid
            e["started"] = min(float(e.get("started", rec["ts"])), rec["ts"])
            e["task"] = dict(e.get("task") or {}, **task)
            if room:
                e["room"] = room
            if done:
                idx.pop(task["id"], None)
                summarize(rec, e, workspace)
                _ack_close(workspace, task["id"])
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
    if done_rec.get("pid"):
        summary["pid"] = done_rec["pid"]
    sp = summaries_path(workspace)
    if done_rec.get("pid") and sp.exists() and _pid_in_log(sp, done_rec["pid"]):
        return summary  # this done row's summary already landed: a replay writes nothing
    with open(sp, "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary


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
