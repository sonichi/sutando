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

This module is the one owner (CLAUDE.md "Shared adapter policy"). Completion
is `find_ready_result`: `local_task_protocol.iter_result_candidates` (every
archive layout, all-digits epoch suffix, live first) walked with
`delivery.readiness.read_ready_result` (whitespace-only is not ready) until a
candidate is READY — an existing empty live placeholder must not hide a ready
archived body. `watch-tasks-stream.sh`'s `handler_result_exists` and every
notifier call it through `has-result`; order is `task_priority`.
Runtime-specific holds stay in the calling bash: the optional-handler probe and
the fallback receipt it is gated on (both need `--runtime`), and the receipt
cleanup.

CLI, for bash callers with only an interpreter path:

    task_dispatch.py has-result <results_dir> <filename>                 # exit 0/1
    task_dispatch.py find-ready <results_dir> <filename>                 # prints path, exit 0/1
    task_dispatch.py pending-candidates <tasks_dir> <results_dir> [--claims-dir D]
    task_dispatch.py next-pending <tasks_dir> <results_dir> [--claims-dir D]
    task_dispatch.py inflight-mark <inflight_dir> <filename> <incarnation>
    task_dispatch.py inflight-live <inflight_dir> <filename> <incarnation>   # exit 0/1
    task_dispatch.py inflight-clear <inflight_dir> <filename>

`inflight-*` is the at-most-once record a notifier keeps between a confirmed submit and a
ready result, keyed to the core incarnation, because terminal history is a lossy record.

`find-ready` exists because "does a ready result exist" and "read what it says" must resolve
to the SAME file: a caller that re-derives the live path after `has-result` says yes can be
answering about an archived body while reading an untouched live placeholder instead.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # lint-workspace-resolution: allow-repo-root

from delivery.readiness import read_ready_result  # noqa: E402

from local_task_protocol import iter_result_candidates  # noqa: E402

from task_priority import sort_tasks_by_priority  # noqa: E402

__all__ = [
    "find_ready_result", "has_ready_result", "find_ready_result_for_filename",
    "pending_candidates", "next_pending_task",
    "mark_inflight", "inflight_is_live", "clear_inflight",
]


def _task_id_for_filename(filename: str) -> str:
    """The one place a task filename is stripped to its id — has-result and find-ready must agree."""
    return filename[:-4] if filename.endswith(".txt") else filename


def find_ready_result(results_dir: "Path | str", task_id: str, *,
                      reader=read_ready_result) -> "Path | None":
    """First result file for `task_id` whose body `reader` accepts, or None.

    Candidates come from `iter_result_candidates` (traversal ids yield none) in
    its precedence, and EVERY one is read until a body is ready: a live
    `results/<id>.txt` can exist empty while the delivered answer sits in an
    archive, and stopping at the first existing path reads that completed task
    as pending. `reader` is the readiness contract, injected so a test can pin
    the walk without re-stating what "ready" means.
    """
    for candidate in iter_result_candidates(Path(results_dir), task_id):
        if reader(candidate) is not None:
            return candidate
    return None


def find_ready_result_for_filename(results_dir: "Path | str", filename: str, *,
                                   reader=read_ready_result) -> "Path | None":
    """`find_ready_result` keyed by task FILENAME (`has_ready_result`'s own id derivation).

    Exists so a caller that already asked `has_ready_result` "does one exist" can ask this
    "which path is it" without re-deriving `task_id` a second, possibly divergent way — and so
    it can read THAT path's body instead of assuming the live one backs every ready result.
    """
    return find_ready_result(results_dir, _task_id_for_filename(filename), reader=reader)


def has_ready_result(results_dir: "Path | str", filename: str) -> bool:
    """True iff task file `filename` has a ready result, live or in any archive layout.

    An empty or whitespace-only file — live or archived — is not a delivery and
    does not stop the search; `find_ready_result` walks past it.
    """
    return find_ready_result_for_filename(results_dir, filename) is not None


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


def _inflight_path(inflight_dir: "Path | str", filename: str) -> Path:
    if not filename or "/" in filename or ".." in filename:
        raise ValueError(f"not a task filename: {filename!r}")
    return Path(inflight_dir) / filename


def mark_inflight(inflight_dir: "Path | str", filename: str, incarnation: str) -> None:
    """Record that `filename`'s prompt was submitted to the core incarnation `incarnation`.

    Terminal history is a lossy record of what was submitted: the prompt scrolls
    out of a small pane and a re-pick after the completion timeout types it again.
    This file is the durable at-most-once record, per core incarnation (a pane pid
    or session stamp); the notifier clears it when a result is ready.
    """
    path = _inflight_path(inflight_dir, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(incarnation.strip() + "\n")
    os.replace(tmp, path)


def inflight_is_live(inflight_dir: "Path | str", filename: str, incarnation: str) -> bool:
    """True while a marker for `filename` names the CURRENT core incarnation.

    A marker from an earlier incarnation is stale (that core is gone and its
    turn with it) and is removed. An unreadable current incarnation ("") keeps
    any marker live: not knowing which core is running is not evidence the
    prompt was never submitted.
    """
    path = _inflight_path(inflight_dir, filename)
    try:
        recorded = path.read_text().strip()
    except FileNotFoundError:
        return False
    current = incarnation.strip()
    if current and recorded and recorded != current:
        path.unlink(missing_ok=True)
        return False
    return True


def clear_inflight(inflight_dir: "Path | str", filename: str) -> None:
    _inflight_path(inflight_dir, filename).unlink(missing_ok=True)


_USAGE = (
    "usage: task_dispatch.py has-result <results_dir> <filename>\n"
    "       task_dispatch.py find-ready <results_dir> <filename>\n"
    "       task_dispatch.py pending-candidates <tasks_dir> <results_dir> [--claims-dir DIR]\n"
    "       task_dispatch.py next-pending <tasks_dir> <results_dir> [--claims-dir DIR]\n"
    "       task_dispatch.py inflight-mark <inflight_dir> <filename> <incarnation>\n"
    "       task_dispatch.py inflight-live <inflight_dir> <filename> <incarnation>   # exit 0/1\n"
    "       task_dispatch.py inflight-clear <inflight_dir> <filename>"
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
    if cmd == "find-ready":
        if rest:
            print(_USAGE, file=sys.stderr)
            return 2
        found = find_ready_result_for_filename(first, second)
        if found is None:
            return 1
        print(found)
        return 0
    if cmd in ("inflight-mark", "inflight-live", "inflight-clear"):
        want = 0 if cmd == "inflight-clear" else 1
        if len(rest) != want:
            print(_USAGE, file=sys.stderr)
            return 2
        try:
            if cmd == "inflight-mark":
                mark_inflight(first, second, rest[0])
                return 0
            if cmd == "inflight-live":
                return 0 if inflight_is_live(first, second, rest[0]) else 1
            clear_inflight(first, second)
            return 0
        except ValueError as exc:
            print(f"task_dispatch.py: {exc}", file=sys.stderr)
            return 2
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
