#!/usr/bin/env python3
"""A task the core handed to an in-session background agent, for a bounded time.

The core may delegate a queued task to a background subagent (docs/subagent-delegation.md).
The task file correctly stays in tasks/ until that agent writes the result, so the Stop
hook would report it as unprocessed at every turn end. A delegation marker says "handed
off at T for S seconds"; while it is live the hook does not report the task, and once it
expires the task blocks again, so an agent that died is still caught. A marker covers only
the turn it was written for: a task file rewritten after it (a wake turn) reads as undelegated.

    python3 src/task_delegation.py mark <task-file-or-id> [--ttl SECONDS] [--workspace DIR]
    python3 src/task_delegation.py check <task-file-or-id> [--task-file PATH] [--workspace DIR]
    python3 src/task_delegation.py clear <task-file-or-id> [--workspace DIR]

Writer contract (this module is the only writer): one JSON file per task under
<workspace>/state/task-delegations/, written atomically (temp file + os.replace), TTL
bounded to [1, MAX_TTL_S]. `mark` exits 0 on a durable write and 1 when it could not
write (the task then keeps blocking, which is the safe side). `check` exits 0 only for a
live marker; absent, unreadable, malformed, expired, future-dated, or older than the task
file (or no task file) is 1. Usage error: 2.

`holder()` is the one "is this task held" predicate for every reader of tasks/ (the Stop
hook through `check`, scripts/unanswered-tasks.py, the orphan classifier): a worker named by
the caller's injected lookup, else BACKGROUND_HOLDER for a live marker, else None.

This is distinct from a worker-pool delivery sentinel (src/delivery/task_dispatch.py):
that hands a task to another runtime instance with no expiry, and notifiers route on it.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_task_protocol import valid_archive_lookup_id  # noqa: E402

DEFAULT_TTL_S = 1800
MAX_TTL_S = 4 * 3600
# Clock skew tolerated before a delegated_at in the future reads as malformed.
_FUTURE_SLACK_S = 5.0
_MAX_MARKER_BYTES = 4096
SUBDIR = Path("state") / "task-delegations"
BACKGROUND_HOLDER = "background-agent"


def _finite(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:  # an int too large for a float, e.g. 10**400
        return False


def _task_id(arg: str) -> str:
    name = Path(arg).name
    return name[:-4] if name.endswith(".txt") else name


def _dir(workspace) -> Path:
    return Path(workspace) / SUBDIR


def marker_path(workspace, task_id: str) -> Path:
    if not valid_archive_lookup_id(task_id):
        raise ValueError(f"not a task id: {task_id!r}")
    return _dir(workspace) / f"{task_id}.json"


def mark(workspace, task_id: str, ttl_s: float = DEFAULT_TTL_S, *, now: float | None = None) -> Path:
    """Record the delegation atomically; raises ValueError on a bad id/TTL, OSError on a failed write."""
    if not _finite(ttl_s) or not 1 <= ttl_s <= MAX_TTL_S:
        raise ValueError(f"ttl must be between 1 and {MAX_TTL_S} seconds")
    path = marker_path(workspace, task_id)
    now = time.time() if now is None else now
    path.parent.mkdir(parents=True, exist_ok=True)
    _prune_expired(path.parent, now)
    body = json.dumps({"task_id": task_id, "task_file": f"tasks/{task_id}.txt",
                       "delegated_at": now, "ttl_s": float(ttl_s)}, separators=(",", ":"))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{task_id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def is_delegated(workspace, task_id: str, *, task_file=None, now: float | None = None) -> bool:
    """True only for a well-formed, unexpired marker written after the task file's last change
    (default <workspace>/tasks/<id>.txt); anything else, a missing task file included, is False."""
    try:
        path = marker_path(workspace, task_id)
        if path.stat().st_size > _MAX_MARKER_BYTES:
            return False
        d = json.loads(path.read_text(encoding="utf-8"))
        task_mtime = Path(task_file or Path(workspace) / "tasks" / f"{task_id}.txt").stat().st_mtime
    except (OSError, ValueError):
        return False
    if not isinstance(d, dict) or d.get("task_id") != task_id:
        return False
    at, ttl = d.get("delegated_at"), d.get("ttl_s")
    if not (_finite(at) and _finite(ttl)) or not 1 <= ttl <= MAX_TTL_S or at < task_mtime:
        return False
    age = (time.time() if now is None else now) - at
    return -_FUTURE_SLACK_S <= age < ttl


def holder(workspace, task_id: str, *, task_file=None, worker_holder=None,
           include_background: bool = True, now: float | None = None) -> str | None:
    """Who holds the task: the injected worker lookup's answer, else BACKGROUND_HOLDER for a
    live marker, else None. Errors from worker_holder propagate (unreadable is not absent)."""
    if worker_holder is not None:
        held = worker_holder(workspace, task_id)
        if held is not None:
            return held
    if include_background and is_delegated(workspace, task_id, task_file=task_file, now=now):
        return BACKGROUND_HOLDER
    return None


def clear(workspace, task_id: str) -> bool:
    try:
        marker_path(workspace, task_id).unlink()
        return True
    except FileNotFoundError:
        return False


def _prune_expired(d: Path, now: float) -> None:
    # Keeps the store bounded: markers expired a day ago carry no signal; pruning is best effort.
    for p in d.glob("*.json"):
        try:
            if now - p.stat().st_mtime > MAX_TTL_S + 86400:
                p.unlink()
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("mark", "check", "clear"):
        p = sub.add_parser(name)
        p.add_argument("task", help="task file path or task id")
        p.add_argument("--workspace", help="workspace root (default: resolved workspace)")
        if name == "check":
            p.add_argument("--task-file", help="the task file the marker must postdate "
                                               "(default: <workspace>/tasks/<id>.txt)")
        if name == "mark":
            p.add_argument("--ttl", type=float, default=DEFAULT_TTL_S,
                           help=f"seconds the hand-off stays live (1..{MAX_TTL_S}, default {DEFAULT_TTL_S})")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 2
    if a.workspace:
        ws = Path(a.workspace)
    else:
        from workspace_default import resolve_workspace
        ws = resolve_workspace(migrate=False)
    tid = _task_id(a.task)
    try:
        if a.cmd == "check":
            return 0 if is_delegated(ws, tid, task_file=a.task_file) else 1
        if a.cmd == "clear":
            clear(ws, tid)
            return 0
        print(mark(ws, tid, a.ttl))
        return 0
    except ValueError as e:
        print(f"task_delegation: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"task_delegation: could not write the marker ({e}); the task keeps blocking", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
