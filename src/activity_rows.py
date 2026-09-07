#!/usr/bin/env python3
"""The agent-activity row writer: one JSON row per line at <workspace>/state/agent-activity.jsonl,
the live window the desktop renders, its per-day archive, the per-task index that keeps a summary
exact after rotation, and the summary left at done.

One owner: the agent-activity skill's CLI and the activity bus both write through here, so the
lock, the rotation, the index and the summary cannot drift between them. Row shape is the contract
the client reads: {"ts", "room", "line", "kind", "task": {"id","from","text","event","into"}, "done"}.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from file_lock import locked_file
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


def _pid_counter(pid: str | None) -> tuple[str | None, int]:
    """(generation, emitted) from a `task:generation:emitted` pid; (None, 0) for a row without one."""
    if not isinstance(pid, str):
        return None, 0
    parts = pid.rsplit(":", 2)
    if len(parts) != 3 or not parts[2].isdigit():
        return None, 0
    return parts[1], int(parts[2])


def _task_rows_on_disk(workspace: Path | None, task_id: str, since: float, until: float) -> tuple[int, dict]:
    """Every row of one task still on disk — the live log and the archive day files its span names —
    with the highest emitted counter per generation. The migration's only exact evidence."""
    live = log_path(workspace)
    names = [live.name, f"{live.stem}.archive.jsonl"]
    day, last = min(since, until), max(since, until)
    while day <= last + 86400 and len(names) < 400:
        names.append(f"{live.stem}.archive.{day_of(day)}.jsonl"); day += 86400
    count, applied, seen = 0, {}, set()
    for name in dict.fromkeys(names):
        try:
            lines = live.with_name(name).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue  # a day the span names but nothing rotated into; any other failure propagates
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if (rec.get("task") or {}).get("id") != task_id:
                continue
            pid = rec.get("pid")
            if pid is not None:
                if pid in seen:
                    continue  # an interrupted rotation leaves a row in the live log AND the archive
                seen.add(pid)
            count += 1
            gen, emitted = _pid_counter(pid)
            if gen is not None:
                applied[gen] = max(int(applied.get(gen, 0)), emitted)
    return count, applied


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


def _pid_in_archive(workspace: Path | None, pid: str, ts: float) -> bool:
    """A landed row whose acknowledgement never landed: once rotated, its only memory is the archive
    day file its own timestamp names (rotate() files by the row's ts)."""
    live = log_path(workspace)
    for name in (f"{live.stem}.archive.{day_of(ts)}.jsonl", f"{live.stem}.archive.jsonl"):
        if _pid_in_log(live.with_name(name), pid):
            return True
    return False



def _pid_in_log(path: Path, pid: str) -> bool:
    try:
        return any(json.loads(l).get("pid") == pid for l in path.read_text(encoding="utf-8").splitlines() if l.strip())
    except (OSError, ValueError):
        return False


def append(line: str, *, kind: str, room: str | None, task: dict | None = None,
           done: bool = False, workspace: Path | None = None, live_rows: int | None = None,
           audience: str | None = None, projection: str | None = None,
           pid: str | None = None, ts: float | None = None, replay: bool = False) -> dict:
    """`pid` is the row's stable projection identity: a replay after a partial write (row appended,
    index or summary not) is applied exactly once, each half checking what already landed."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    rec: dict = {"ts": ts if ts is not None else time.time(), "line": line.strip(), "kind": kind}
    if pid:
        rec["pid"] = pid
    if audience:
        rec["audience"] = audience
    if projection:
        rec["projection"] = projection
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
        task_id = task.get("id") if isinstance(task, dict) and isinstance(task.get("id"), str) else None
        acked = bool(pid and task_id and (_pid_acked(workspace, task_id, pid)
                                          or (done and _pid_in_log(summaries_path(workspace), pid))))
        # Only a replay (the bus retrying an owed row) consults the archive: exact for recovery, and a
        # fresh row never reads the day file under the lock.
        landed = bool(pid) and (acked or _pid_in_log(path, pid) or (replay and _pid_in_archive(workspace, pid, rec["ts"])))
        if not landed:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if pid and task_id:
                _ack(workspace, task_id, pid)
        if task and isinstance(task.get("id"), str):
            ip = index_path(workspace)
            idx = _load_index(ip)
            existing = idx.get(task["id"])
            e = existing or {"started": rec["ts"], "rows": 0, "task": task, "room": room}
            # A row that landed just now is new by construction; a replay counts only above the
            # generation's applied high-water mark, saved with the count in the same record.
            gen, emitted = _pid_counter(pid)
            applied = dict(e.get("applied") or {})
            if existing is not None and "applied" not in existing:
                # The previous writer counted a row only when its index save landed, so neither the
                # entry nor a landed row says which rows it counted: rebuild once from the rows on disk.
                e["rows"], applied = _task_rows_on_disk(workspace, task["id"], float(e.get("started", rec["ts"])), max(rec["ts"], time.time()))
            elif not landed or emitted > int(applied.get(gen, 0)):
                e["rows"] = int(e.get("rows", 0)) + 1
            if gen is not None:
                applied[gen] = max(int(applied.get(gen, 0)), emitted)
            e["applied"] = applied
            e.pop("last_pid", None)
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
