#!/usr/bin/env python3
"""Consumer-side dispatch policy shared by every external task-notifier.

`src/watch-tasks-stream.sh` is the detection layer for all core runtimes and
emits `TASK_FILE: <name>` wakes. The external notifiers (Codex, agy, Claude
standalone) consume those wakes, and each one answers the same two questions
of the same on-disk state: "does this task already have a delivered result?"
and "which pending task goes next?". Three hand-rolled bash copies answered
them, and the copies were wrong the same way: `[ -f results/<f> ]` read an
empty placeholder as delivered, and `find -name "<stem>-[0-9]*.txt"` took
another task's `task-x-1-other.txt` for `task-x`'s epoch archive.

This module is the one owner (CLAUDE.md "Shared adapter policy"). It decides
nothing itself: completion is `local_task_protocol.find_result` (every archive
layout, all-digits epoch suffix) plus `delivery.readiness.read_ready_result`
(whitespace-only is not ready) — the same pair `watch-tasks-stream.sh`'s
`handler_result_exists` calls — and order is `task_priority`. Runtime-specific
holds stay in the calling bash: the optional-handler probe and the fallback
receipt it is gated on (both need `--runtime`), and the receipt cleanup.

CLI, for bash callers with only an interpreter path:

    task_dispatch.py has-result <results_dir> <filename>                 # exit 0/1
    task_dispatch.py pending-candidates <tasks_dir> <results_dir> [--claims-dir D]
    task_dispatch.py next-pending <tasks_dir> <results_dir> [--claims-dir D]
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # lint-workspace-resolution: allow-repo-root

from delivery.readiness import read_ready_result  # noqa: E402

from local_task_protocol import find_result  # noqa: E402

from task_priority import sort_tasks_by_priority  # noqa: E402

__all__ = ["has_ready_result", "pending_candidates", "next_pending_task"]


def has_ready_result(results_dir: "Path | str", filename: str) -> bool:
    """True iff task file `filename` has a ready result, live or in any archive layout.

    `find_result` rejects traversal ids and returns None; a found file must
    still pass `read_ready_result`, so an empty or whitespace-only file — live
    or archived — is not a delivery.
    """
    task_id = filename[:-4] if filename.endswith(".txt") else filename
    found = find_result(Path(results_dir), task_id)
    return found is not None and read_ready_result(found) is not None


def pending_candidates(
    tasks_dir: "Path | str",
    results_dir: "Path | str",
    *,
    claims_dir: "Path | str | None" = None,
) -> Iterator[str]:
    """Task filenames without a ready result, priority-sorted (mtime FIFO within a tier).

    A name under `claims_dir` is held by a watcher-owned handler and skipped.
    Only regular files are yielded, never a name that carries a path separator
    or traversal sentinel, whatever the sort step handed back.
    """
    claims = Path(claims_dir) if claims_dir else None
    for task in sort_tasks_by_priority(Path(tasks_dir).glob("*.txt")):
        if not task.is_file():
            continue
        name = task.name
        if not name or "/" in name or ".." in name:
            continue
        if has_ready_result(results_dir, name):
            continue
        if claims is not None and (claims / name).is_file():
            continue
        yield name


def next_pending_task(
    tasks_dir: "Path | str",
    results_dir: "Path | str",
    *,
    claims_dir: "Path | str | None" = None,
) -> str | None:
    """First entry of `pending_candidates`, or None."""
    for name in pending_candidates(tasks_dir, results_dir, claims_dir=claims_dir):
        return name
    return None


_USAGE = (
    "usage: task_dispatch.py has-result <results_dir> <filename>\n"
    "       task_dispatch.py pending-candidates <tasks_dir> <results_dir> [--claims-dir DIR]\n"
    "       task_dispatch.py next-pending <tasks_dir> <results_dir> [--claims-dir DIR]"
)


def _parse_claims_dir(rest: list[str]) -> "str | None":
    """`[--claims-dir DIR]` after the two positional dirs; anything else is a usage error."""
    if not rest:
        return None
    if len(rest) == 2 and rest[0] == "--claims-dir" and rest[1]:
        return rest[1]
    raise ValueError(_USAGE)


def _main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(_USAGE, file=sys.stderr)
        return 2
    cmd, first, second, rest = argv[0], argv[1], argv[2], argv[3:]
    if cmd == "has-result":
        if rest:
            print(_USAGE, file=sys.stderr)
            return 2
        return 0 if has_ready_result(first, second) else 1
    if cmd not in ("pending-candidates", "next-pending"):
        print(f"task_dispatch.py: unknown command {cmd!r}\n{_USAGE}", file=sys.stderr)
        return 2
    try:
        claims_dir = _parse_claims_dir(rest)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if cmd == "pending-candidates":
        names = list(pending_candidates(first, second, claims_dir=claims_dir))
        for name in names:
            print(name)
        return 0 if names else 1
    name = next_pending_task(first, second, claims_dir=claims_dir)
    if name is None:
        return 1
    print(name)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
