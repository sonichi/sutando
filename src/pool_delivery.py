#!/usr/bin/env python3
"""Delivery-side half of the worker pool: read one recipient's own folder.

Stage 1 of docs/worker-pool-design.md. A recipient — the core, or later a
worker — receives work as *sentinels* in `deliveries/<recipient>/`:

    tasks/<task-id>.txt                      the payload; immutable, never copied
    deliveries/<me>/<task-id>.txt            a sentinel; existing IS the assignment
    deliveries/<me>/<task-id>.accepted       the same sentinel, suffix substituted
    state/workers/<me>/done/<task-id>.flag   completion evidence; ONE writer,
                                            `mark_done`, run BEFORE the result

That flag has two readers — `residue` here, and `src/result_claimant.py`, which
the gateway consults to stamp an outbound reply with the worker that produced
it. Hence the ordering: flag, then result.

Creating the sentinel assigns (the router's job, not this module's). Renaming it
records that the ASSIGNED recipient accepted the work — a worker never selects
its own, so nothing is claimed here. The rename is atomic and exclusive, so a
losing racer sees OSError and walks away. Nothing here writes a sentinel, reads another recipient's folder, or
selects work that was not delivered.

This module is intentionally free of any watcher, session or transport concern:
it is the logic a reader needs, so the same rules hold whether events arrive by
fswatch, by poll, or not at all.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workspace_default import resolve_workspace  # noqa: E402

from delivery.readiness import read_ready_result  # noqa: E402

# `.txt` because the watcher that wakes a worker emits for no other extension.
PENDING_SUFFIX = ".txt"
ACCEPTED_SUFFIX = ".accepted"
# Sentinels written before the rename. Recognised so work already accepted under
# the old name is never re-delivered; nothing writes this suffix.
LEGACY_ACCEPTED_SUFFIX = ".claimed"
# Not a sentinel: the regex below never matches it, so listings skip it.
LOCK_NAME = ".lock"

# One suffix, substituted never appended: an accepted sentinel must not still
# read as pending, or a reader re-takes its own in-flight work.

# `~`: the bridge encodes a channel instance into the id (task-<inst>~<id>).
_SENTINEL = re.compile(
    r"^(?P<id>task-[A-Za-z0-9_~-]+?)(?:\.txt|(?P<accepted>\.accepted|\.claimed))$")

RECIPIENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class NotDelivered(Exception):
    """The sentinel named is absent, malformed, or not this recipient's."""


def parse_sentinel(name: str) -> tuple[str, bool] | None:
    """(task_id, accepted) for a sentinel filename, or None if it is not one."""
    m = _SENTINEL.match(name)
    if not m:
        return None
    return m.group("id"), m.group("accepted") is not None


def _root(workspace) -> Path:
    """`None` resolves through the one sanctioned helper — a second resolution
    path is how a reader and a writer end up in different trees."""
    return Path(workspace) if workspace is not None else resolve_workspace()


def deliveries_dir(workspace, recipient: str) -> Path:
    if not RECIPIENT.match(recipient):
        raise ValueError(f"recipient id must match {RECIPIENT.pattern!r}: {recipient!r}")
    return _root(workspace) / "deliveries" / recipient


def payload_path(workspace: Path, task_id: str) -> Path:
    return _root(workspace) / "tasks" / f"{task_id}{PENDING_SUFFIX}"


def archived_payload(workspace: Path, task_id: str) -> Path:
    return _root(workspace) / "tasks" / "archive" / f"{task_id}{PENDING_SUFFIX}"


def results_dir(workspace) -> Path:
    """Where every recipient's answers go. Named here so a caller that only
    composes the path (a spawner, a launcher) does not re-spell the layout."""
    return _root(workspace) / "results"


def result_path(workspace: Path, task_id: str) -> Path:
    return results_dir(workspace) / f"{task_id}.txt"


def done_flag(workspace: Path, recipient: str, task_id: str) -> Path:
    return _root(workspace) / "state" / "workers" / recipient / "done" / f"{task_id}.flag"


def is_done_flag(path) -> bool:
    """Completion evidence is a REGULAR file and nothing else: a directory at
    the name is malformed state, and reading it as a finish invents a claimant.

    Only absence answers False quietly. Any other stat error propagates — a
    tree that cannot be read is not an empty tree, and a reader deciding that
    for itself is how one claimant hides and another gets stamped.
    """
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat.S_ISREG(st.st_mode)


def mark_done(workspace, recipient: str, task_id: str) -> Path:
    """Record that `recipient` finished `task_id`. The ONLY writer of the flag.

    Ordering is flag THEN result: the gateway reads attribution off this file
    when it drains `results/`, so a result published first is delivered with no
    worker on it. The reverse — a flag with no result — is recoverable, and
    `residue` maps it to `finished`.

    Write is temp-file + rename inside the same directory, so a concurrent
    reader sees the name either absent or complete, never half-written.
    """
    if not RECIPIENT.match(recipient):
        raise ValueError(f"recipient id must match {RECIPIENT.pattern!r}: {recipient!r}")
    if not _SENTINEL.match(task_id + PENDING_SUFFIX):
        raise ValueError(f"not a task id: {task_id!r}")
    dst = done_flag(workspace, recipient, task_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.", suffix=".tmp")
    os.close(fd)
    try:
        os.replace(tmp, dst)
    except OSError:
        os.unlink(tmp)
        raise
    return dst


def pending(workspace: Path, recipient: str) -> list[Path]:
    """Pending sentinels in this recipient's folder, oldest first.

    Ordering is by the sentinel's own mtime, so a reader drains in delivery
    order without reading any payload.
    """
    d = deliveries_dir(workspace, recipient)
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        got = parse_sentinel(p.name)
        if got and not got[1]:
            out.append(p)
    return sorted(out, key=lambda q: (q.stat().st_mtime, q.name))


def accepted(workspace: Path, recipient: str) -> list[Path]:
    d = deliveries_dir(workspace, recipient)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if (got := parse_sentinel(p.name)) and got[1])


def find(workspace: Path, recipient: str, task_id: str) -> Path | None:
    """The sentinel for `task_id` under either name, or None."""
    d = deliveries_dir(workspace, recipient)
    for name in (task_id + PENDING_SUFFIX, task_id + ACCEPTED_SUFFIX,
                 task_id + LEGACY_ACCEPTED_SUFFIX):
        p = d / name
        if p.exists():
            return p
    return None


# Third answer for --held, kept out of the 0/1 pair so an unreadable delivery
# tree cannot be read as "nobody holds this".
HELD_UNKNOWN = 2


def held_by_worker(workspace, task_id: str) -> str | None:
    """The worker holding `task_id` under ANY sentinel name, or None.

    One grammar for "a worker already has this", legacy `.claimed` included —
    a second spelling elsewhere reads accepted work as unprocessed.
    """
    root = _root(workspace) / "deliveries"
    if not root.is_dir():
        return None
    for d in sorted(root.iterdir()):
        # The core's own inbox is not a hold: the core declining is what put
        # the task in front of the Stop hook in the first place.
        if not d.is_dir() or d.name == "core" or not RECIPIENT.match(d.name):
            continue
        if find(workspace, d.name, task_id) is not None:
            return d.name
    return None


@contextlib.contextmanager
def arbitration(workspace, recipient: str):
    """One lock per folder around publish, accept and release, so a check of
    both names and the rename that follows it cannot interleave."""
    d = deliveries_dir(workspace, recipient)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / LOCK_NAME, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def accept(sentinel: Path) -> Path:
    """Take up work already assigned here. Atomic and exclusive; a loser gets
    OSError.

    Nothing is selected: the folder decided the recipient. This only excludes a
    second incarnation of the SAME recipient — never a sibling, which cannot see
    this folder.
    """
    got = parse_sentinel(sentinel.name)
    if got is None or got[1]:
        raise NotDelivered(f"not a pending sentinel: {sentinel.name}")
    dst = sentinel.with_name(got[0] + ACCEPTED_SUFFIX)
    with arbitration(sentinel.parent.parent.parent, sentinel.parent.name):
        # A stray pending name beside the accepted one: rename would silently
        # replace work in flight. (A racing loser's source is gone: OSError.)
        if dst.exists() and sentinel.exists():
            raise NotDelivered(f"already accepted: {dst.name}")
        os.rename(sentinel, dst)
    return dst


def release(sentinel: Path) -> Path:
    """Hand an accepted delivery back to the SAME recipient for its next run."""
    got = parse_sentinel(sentinel.name)
    if got is None or not got[1]:
        raise NotDelivered(f"not an accepted sentinel: {sentinel.name}")
    dst = sentinel.with_name(got[0] + PENDING_SUFFIX)
    with arbitration(sentinel.parent.parent.parent, sentinel.parent.name):
        os.rename(sentinel, dst)
    return dst


def residue(workspace: Path, recipient: str, task_id: str) -> str:
    """What a crash left behind, read without a journal.

    One name per state, each mapped by `sweep` to exactly one action.
    """
    ws = Path(workspace)
    # The shared readiness contract, not existence: an empty or whitespace-only
    # file is a placeholder still being written, and must not suppress recovery.
    has_result = read_ready_result(result_path(ws, task_id)) is not None
    has_flag = is_done_flag(done_flag(ws, recipient, task_id))
    sentinel = find(ws, recipient, task_id)
    payload = payload_path(ws, task_id).is_file()

    # The flag is terminal on its own: the bridge drains the result file on
    # delivery, so after a drain the flag is the only durable evidence left.
    if has_flag:
        return "finished"
    if has_result:
        return "completed"
    if sentinel is None:
        if payload and not archived_payload(ws, task_id).is_file():
            return "undelivered"
        return "clean"
    if not payload:
        return "stale-sentinel"
    return "died-mid-work" if parse_sentinel(sentinel.name)[1] else "pending"


def sweep(workspace: Path, recipient: str) -> dict:
    """Boot reconciliation. An event that fired while nobody listened is gone,
    so a reader that only streams never learns about it."""
    ws = Path(workspace)
    seen = set()
    actions = {"ready": [], "released": [], "completed": [], "retired": [], "stale": []}
    for p in accepted(ws, recipient) + pending(ws, recipient):
        task_id = parse_sentinel(p.name)[0]
        if task_id in seen:
            continue
        seen.add(task_id)
        state = residue(ws, recipient, task_id)
        if state == "pending":
            actions["ready"].append(task_id)
        elif state == "died-mid-work":
            release(p)
            actions["released"].append(task_id)
        elif state == "completed":
            actions["completed"].append(task_id)
        elif state == "finished":
            # Both result and flag exist, so the delivery is spent. Leaving the
            # sentinel would hand finished work back as ready on the next boot.
            p.unlink()
            actions["retired"].append(task_id)
        elif state == "stale-sentinel":
            p.unlink()
            actions["stale"].append(task_id)
        else:
            raise AssertionError(f"unmapped residue state {state!r} for {task_id}")
    return actions


def read_payload(workspace: Path, task_id: str) -> str | None:
    """The task text from `tasks/`, which a sentinel only points at.

    The bridges write header-format text, never JSON; a reader that wants
    fields parses it with local_task_protocol, not here.
    """
    p = payload_path(Path(workspace), task_id)
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _emit(task_id: str) -> None:
    # A dead consumer must end the reader rather than let events pile up unseen.
    try:
        print(f"DELIVERY: {task_id}", flush=True)
    except BrokenPipeError:
        raise SystemExit(0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="read one recipient's delivery folder")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--recipient", default="core")
    ap.add_argument("command", nargs="?",
                    choices=("sweep", "pending", "watch", "residue", "payload",
                             "mark-done"))
    ap.add_argument("--task-id")
    ap.add_argument("--sentinel", help="a sentinel filename, for `payload`")
    ap.add_argument("--held", metavar="TASK_ID",
                    help="exit 0 if a worker holds this task, 1 if none does, "
                         f"{HELD_UNKNOWN} if that could not be determined")
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args(argv)
    ws = Path(a.workspace)

    if a.held:
        try:
            holder = held_by_worker(ws, a.held)
        except Exception as e:
            # A crash and "no worker holds it" are different answers; a caller
            # that cannot tell them apart reads a broken lookup as a free task.
            print(f"pool_delivery: --held {a.held}: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return HELD_UNKNOWN
        if holder:
            print(holder)
        return 0 if holder else 1
    if not a.command:
        ap.error("a command is required unless --held")

    if a.command == "payload":
        # A sentinel names a payload and holds none. Callers outside python ask
        # here rather than re-spelling the name grammar or the tasks/ layout.
        task_id = a.task_id
        if not task_id:
            if not a.sentinel:
                ap.error("--sentinel or --task-id is required for payload")
            got = parse_sentinel(a.sentinel)
            if got is None:
                print(f"pool_delivery: not a sentinel name: {a.sentinel}", file=sys.stderr)
                return 1
            task_id = got[0]
        p = payload_path(ws, task_id)
        if not p.is_file():
            print(f"pool_delivery: no payload for {task_id} at {p}", file=sys.stderr)
            return 1
        print(p)
        return 0
    if a.command == "mark-done":
        if not a.task_id:
            ap.error("--task-id is required for mark-done")
        print(mark_done(ws, a.recipient, a.task_id))
        return 0
    if a.command == "residue":
        if not a.task_id:
            ap.error("--task-id is required for residue")
        print(residue(ws, a.recipient, a.task_id))
        return 0
    if a.command == "pending":
        for p in pending(ws, a.recipient):
            print(parse_sentinel(p.name)[0])
        return 0
    if a.command == "sweep":
        print(json.dumps(sweep(ws, a.recipient), indent=2))
        return 0

    # A release puts the name back as pending, and pending-at-boot is exactly
    # what a streaming reader never hears about: announce both.
    boot = sweep(ws, a.recipient)
    for task_id in boot["ready"] + boot["released"]:
        _emit(task_id)
    announced = {parse_sentinel(p.name)[0] for p in pending(ws, a.recipient)}
    while True:
        time.sleep(a.interval)
        now = {parse_sentinel(p.name)[0] for p in pending(ws, a.recipient)}
        for task_id in sorted(now - announced):
            _emit(task_id)
        announced = now


if __name__ == "__main__":
    sys.exit(main())
