#!/usr/bin/env python3
"""What happened to a task whose file is not where a notification said it was.

A task named by a watcher notification can be gone by the time it is read: a lane
claimed it, a bridge archived it after delivery, or it was answered. None of those
is missing state, so a reader resolves the id here before treating it as an error.

    python3 src/task_disposition.py <task-id> [--workspace DIR]

Prints one word: pending | claimed | answered | archived | missing (exit 0), or
exits 2 on an id that cannot be looked up. Only `missing` is unexplained, and even
that is the owner's to read in a log or the owner's DM, never a task conversation.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from delivery.task_dispatch import find_ready_result
from local_task_protocol import find_archived_task, valid_archive_lookup_id  # noqa: E402
from task_archive import find_task_file  # noqa: E402

PENDING, CLAIMED, ANSWERED, ARCHIVED, MISSING = "pending", "claimed", "answered", "archived", "missing"
SETTLED = frozenset((ANSWERED, ARCHIVED))


def task_disposition(workspace, task_id: str) -> str:
    """The task's state, answered first: a ready result anywhere settles it whatever the task file says."""
    if not valid_archive_lookup_id(task_id):
        raise ValueError("not a task id")
    root = Path(workspace)
    tasks, results = root / "tasks", root / "results"
    if find_ready_result(results, task_id) is not None:
        return ANSWERED
    live = find_task_file(tasks, task_id)
    if live is not None:
        return PENDING if live.name == f"{task_id}.txt" else CLAIMED
    if find_archived_task(tasks, task_id) is not None:
        return ARCHIVED
    return MISSING


def _task_id(arg: str) -> str:
    name = Path(arg).name
    return name[:-4] if name.endswith(".txt") else name


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("task", help="task id or task file name")
    ap.add_argument("--workspace", help="workspace root (default: resolved workspace)")
    args = ap.parse_args(argv)
    if args.workspace:
        workspace = Path(args.workspace)
    else:
        from workspace_default import resolve_workspace
        workspace = resolve_workspace(migrate=False)
    try:
        print(task_disposition(workspace, _task_id(args.task)))
    except ValueError as e:
        print(f"task_disposition: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
