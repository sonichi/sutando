#!/usr/bin/env python3
"""The pending task queue, in one place: which task files are waiting, in the order the core will
take them, and where a given task stands in that order.

`pending(workspace)` lists <workspace>/tasks/task-*.txt minus the core's own bookkeeping files
(task-cron-*, task-bench-*, task-workstream-*, task-project-grouping-*), sorted the way the
consumer sorts (task_priority: urgent → normal → low, then oldest first). `position(workspace, id)`
is {depth, position}: how many are pending and this task's 1-based rank (0 when it is not pending).
`write_snapshot(workspace)` publishes the same list to <workspace>/state/task-queue.json — the
fresh queue for readers (voice's get_core_status, the desktop). core-status.json stays its own
single-writer record: its `ts` is a liveness gate, so the queue does not ride on it.

Only an absent tasks/ (or a file where the dir should be) is an empty queue. Any other failure to
read it — permissions above all — raises: a queue that could not be counted is never reported as
zero, and `write_snapshot` then leaves the previous snapshot in place rather than publish one.

CLI (every command takes --workspace; --task-file derives it from the file's own tasks/ dir;
--inbox names the announcing watcher's directory when it is not tasks/, a pool worker's delivery
folder, and the count is then what waits THERE, never the core's queue):
  task_queue.py pending [--inbox D]              the list, JSON
  task_queue.py position --task-file P [--inbox D]  {depth, position}, JSON
  task_queue.py waiting --task-file P [--inbox D]   how many OTHER tasks are pending, a bare integer,
                                                 and (without --inbox) the snapshot is refreshed
  task_queue.py snapshot                         write state/task-queue.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Self-sufficient import, like task_priority: shell callers load this file by path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_priority import parse_priority_from_file, sort_tasks_by_priority  # noqa: E402
from workspace_default import resolve_workspace, write_status  # noqa: E402

BOOKKEEPING_PREFIXES = ("task-cron-", "task-bench-", "task-workstream-", "task-project-grouping-")
SNAPSHOT_NAME = "task-queue.json"


def is_queue_task(name: str) -> bool:
    """A task file the owner is waiting on: task-*.txt that is not the core's own bookkeeping."""
    stem = name[:-4] if name.endswith(".txt") else name
    return stem.startswith("task-") and not stem.startswith(BOOKKEEPING_PREFIXES)


def _task_id(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition(":")
            if key == "id" and sep and value.strip():
                return value.strip()
            if key == "task" and sep:
                break
    except OSError:
        pass
    return path.stem


def _source(path: Path) -> str | None:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition(":")
            if key == "source" and sep and value.strip():
                return value.strip()
            if key == "task" and sep:
                break
    except OSError:
        pass
    return None


def pending(workspace: Path | None = None, inbox: Path | str | None = None) -> list[dict]:
    """[{id, source, priority, since}] in consumption order. A missing tasks/ dir is an empty queue;
    one that exists but cannot be read raises (PermissionError and the rest), never reads as empty.

    `inbox` is the directory the announcing watcher reads, when that is not the workspace's tasks/:
    a pool worker's delivery folder, whose entries are sentinels (a name and nothing else). The
    queue is what waits in THAT inbox; without it, the core's tasks/ is the queue."""
    ws = workspace or resolve_workspace()
    tasks_dir = Path(inbox) if inbox else ws / "tasks"
    try:
        paths = [p for p in tasks_dir.iterdir() if p.is_file() and is_queue_task(p.name)]
    except (FileNotFoundError, NotADirectoryError):
        return []
    if inbox:
        # A sentinel is never retired on a live host: an entry is pending only while its task
        # has no ready result, resolved for the whole inbox at once (listed, not globbed per entry).
        from delivery.task_dispatch import ready_result_filenames
        paths = [p for p in paths if p.name.endswith(".txt")]
        ready = ready_result_filenames(ws / "results", [p.name for p in paths])
        paths = [p for p in paths if p.name not in ready]
    out = []
    for p in sort_tasks_by_priority(paths):
        try:
            since = int(p.stat().st_mtime)
        except OSError:
            since = 0
        out.append({"id": _task_id(p), "source": _source(p), "priority": parse_priority_from_file(p), "since": since})
    return out


def position(workspace: Path | None, task_id: str, inbox: Path | str | None = None) -> dict:
    """{depth, position}: position is 1-based in consumption order, 0 when the task is not pending.
    Raises like `pending` when tasks/ cannot be read: there is no depth to report then."""
    queue = pending(workspace, inbox)
    ids = [t["id"] for t in queue]
    return {"depth": len(queue), "position": ids.index(task_id) + 1 if task_id in ids else 0}


def waiting(workspace: Path | None, task_id: str, inbox: Path | str | None = None) -> int:
    """How many other tasks are pending besides this one: the QUEUE line's number, counted in the
    announcing inbox. Raises like `pending` when it cannot be read: the line is then not printed
    rather than printed as 0."""
    pos = position(workspace, task_id, inbox)
    return pos["depth"] - (1 if pos["position"] else 0)


def write_snapshot(workspace: Path | None = None) -> Path | None:
    """Atomically publish {ts, depth, pending} to <workspace>/state/task-queue.json. When tasks/
    cannot be read nothing is published — the previous snapshot stays as it was (readers date it
    by `ts`, so it ages into "unknown"), one line goes to stderr, and None is returned."""
    ws = workspace or resolve_workspace()
    try:
        queue = pending(ws)
    except OSError as exc:
        print(f"task_queue: snapshot not published, tasks/ could not be read: {exc}", file=sys.stderr)
        return None
    return write_status(SNAPSHOT_NAME, {"ts": int(time.time()), "depth": len(queue), "pending": queue}, workspace=ws)


def _workspace_of(task_file: str | None, workspace: str | None) -> Path | None:
    if workspace:
        return Path(workspace)
    if task_file:
        return Path(task_file).resolve().parent.parent
    return None


def main(argv: list[str] | None = None) -> int:
    """Exits 0 on every path: a caller in the dispatch path must never fail because the queue could
    not be counted; a count it cannot give is not printed, and the reason goes to stderr."""
    import argparse
    ap = argparse.ArgumentParser(description="the pending task queue")
    ap.add_argument("cmd", choices=("pending", "position", "waiting", "snapshot"))
    ap.add_argument("--task-file"); ap.add_argument("--task-id"); ap.add_argument("--workspace")
    ap.add_argument("--inbox", help="the announcing watcher's directory when it is not <workspace>/tasks")
    try:
        a = ap.parse_args(argv)
        ws = _workspace_of(a.task_file, a.workspace)
        tid = a.task_id or (_task_id(Path(a.task_file)) if a.task_file else None)
        # The core's watcher names its inbox too, and its inbox IS tasks/: that is the
        # no-inbox case, snapshot and all, not a worker's.
        inbox = a.inbox or None
        if inbox and ws is not None and Path(inbox).resolve() == (ws / "tasks").resolve():
            inbox = None
        if a.cmd == "pending":
            print(json.dumps(pending(ws, inbox)))
        elif a.cmd == "snapshot":
            written = write_snapshot(ws)
            if written is not None:
                print(str(written))
        elif not tid:
            return 0
        elif a.cmd == "position":
            print(json.dumps(position(ws, tid, inbox)))
        else:
            n = waiting(ws, tid, inbox)
            # The snapshot is the core's queue; a worker inbox's count never overwrites it.
            if not inbox:
                write_snapshot(ws)
            print(n)
    except SystemExit:
        return 0
    except Exception as exc:  # noqa: BLE001 - never fail the dispatch path
        print(f"task_queue: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
