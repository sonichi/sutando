#!/usr/bin/env python3
"""Shared candidate-selection policy for task-file-injection watchers.

Codex's and agy's `task-notifier.sh` both ask the same two questions of the
same on-disk state: "does this task already have a delivered result?" and
"which pending task should be dispatched next?". Each notifier used to answer
both with its own hand-rolled bash, and the copies diverged from the correct
answer: a zero-byte partial result read as delivered (should not — readiness
is `delivery.readiness`'s contract), and a `find -name "$stem-[0-9]*.txt"`
glob matched `task-probe-1-other.txt` as an epoch archive of `task-probe`
(should not — `local_task_protocol.find_result`'s epoch-suffix match requires
the WHOLE suffix to be digits, not just its first character).

This module is the one place both delegate to (CLAUDE.md's "Shared adapter
policy" rule: two adapters interpreting the same workspace state get a
dependency-light `src/` module, never a second copy). Provider-specific
extras — Codex's optional-task-handler claim probe, a fallback-receipt
cleanup side effect — stay in the calling bash, at the adapter edge.

Callable as a library (`has_ready_result`, `pending_candidates`,
`next_pending_task`) or as a CLI for bash callers that only have a python3
binary to shell out to:

    task_dispatch.py has-result <results_dir> <filename>       # exit 0/1
    task_dispatch.py pending-candidates <tasks_dir> <results_dir>  # one name/line
    task_dispatch.py next-pending <tasks_dir> <results_dir>     # first name, exit 0/1
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

# Bash callers shell out to this file directly (not `-m`) — same
# self-sufficient sys.path bootstrap task_priority.py uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from delivery.readiness import read_ready_result  # noqa: E402

from local_task_protocol import find_result, valid_archive_lookup_id  # noqa: E402

from task_priority import sort_tasks_by_priority  # noqa: E402

__all__ = ["has_ready_result", "pending_candidates", "next_pending_task"]


def has_ready_result(results_dir: "Path | str", filename: str) -> bool:
    """True iff `filename` (a task file's basename, e.g. `task-x.txt`) has a
    delivered result: a non-empty live `results/<filename>`, or an archived
    copy under any of the archive layouts `local_task_protocol` knows about.

    Delegates path resolution entirely to `local_task_protocol.find_result`
    (live-then-archive, correct digits-only epoch-suffix matching) and the
    live-file readiness check to `delivery.readiness.read_ready_result` (a
    zero-byte or partial write is not a delivered result). An archived path
    is a completed delivery by construction — archival trails a ready live
    write, so it needs no re-check.
    """
    task_id = filename[:-4] if filename.endswith(".txt") else filename
    if not valid_archive_lookup_id(task_id):
        return False
    found = find_result(results_dir, task_id)
    if found is None:
        return False
    live = Path(results_dir) / filename
    if found == live:
        return read_ready_result(found) is not None
    return True


def pending_candidates(tasks_dir: "Path | str", results_dir: "Path | str") -> Iterator[str]:
    """Task filenames in `tasks_dir`, priority-sorted (mtime-FIFO within a
    tier), that do not yet have a delivered result. A provider-specific
    notifier applies its own additional holds (an optional-handler claim, a
    Team-tier probe) on top of this — this generator answers only the
    provider-neutral half: priority order + completion.
    """
    for task in sort_tasks_by_priority(Path(tasks_dir).glob("*.txt")):
        if not task.is_file():
            continue
        name = task.name
        if not name or "/" in name or ".." in name:
            continue
        if has_ready_result(results_dir, name):
            continue
        yield name


def next_pending_task(tasks_dir: "Path | str", results_dir: "Path | str") -> str | None:
    """First entry of `pending_candidates`, or None."""
    for name in pending_candidates(tasks_dir, results_dir):
        return name
    return None


def _main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: task_dispatch.py {has-result <results_dir> <filename>"
              " | pending-candidates <tasks_dir> <results_dir>"
              " | next-pending <tasks_dir> <results_dir>}", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "has-result":
        return 0 if has_ready_result(argv[1], argv[2]) else 1
    if cmd == "pending-candidates":
        names = list(pending_candidates(argv[1], argv[2]))
        for name in names:
            print(name)
        return 0 if names else 1
    if cmd == "next-pending":
        name = next_pending_task(argv[1], argv[2])
        if name is None:
            return 1
        print(name)
        return 0
    print(f"task_dispatch.py: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
