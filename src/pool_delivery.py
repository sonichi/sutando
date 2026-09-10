#!/usr/bin/env python3
"""Delivery-side half of the worker pool: read one recipient's own folder.

Stage 1 of docs/worker-pool-design.md. A recipient — the core, or later a
worker — receives work as *sentinels* in `deliveries/<recipient>/`:

    tasks/<task-id>.txt                      the payload; immutable, never copied
    deliveries/<me>/<task-id>.txt            a sentinel; existing IS the assignment
    deliveries/<me>/<task-id>.claimed        the same sentinel, suffix substituted

Creating the sentinel assigns (the router's job, not this module's). Renaming it
claims, which is atomic and exclusive, so a losing racer sees OSError and walks
away. Nothing here writes a sentinel, reads another recipient's folder, or
selects work that was not delivered.

This module is intentionally free of any watcher, session or transport concern:
it is the logic a reader needs, so the same rules hold whether events arrive by
fswatch, by poll, or not at all.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workspace_default import resolve_workspace  # noqa: E402

# `.txt` because the watcher that wakes a worker emits for no other extension.
PENDING_SUFFIX = ".txt"
CLAIMED_SUFFIX = ".claimed"

# One suffix, substituted never appended: a claimed sentinel must not still read
# as unclaimed, or a reader re-takes its own in-flight work.
_SENTINEL = re.compile(
    r"^(?P<id>task-[A-Za-z0-9_-]+?)(?:\.txt|(?P<claimed>\.claimed))$")

RECIPIENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class NotDelivered(Exception):
    """The sentinel named is absent, malformed, or not this recipient's."""


def parse_sentinel(name: str) -> tuple[str, bool] | None:
    """(task_id, claimed) for a sentinel filename, or None if it is not one."""
    m = _SENTINEL.match(name)
    if not m:
        return None
    return m.group("id"), m.group("claimed") is not None


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


def result_path(workspace: Path, task_id: str) -> Path:
    return _root(workspace) / "results" / f"{task_id}.txt"


def done_flag(workspace: Path, recipient: str, task_id: str) -> Path:
    return _root(workspace) / "state" / "workers" / recipient / "done" / f"{task_id}.flag"


def pending(workspace: Path, recipient: str) -> list[Path]:
    """Unclaimed sentinels in this recipient's folder, oldest first.

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


def claimed(workspace: Path, recipient: str) -> list[Path]:
    d = deliveries_dir(workspace, recipient)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if (got := parse_sentinel(p.name)) and got[1])


def find(workspace: Path, recipient: str, task_id: str) -> Path | None:
    """The sentinel for `task_id` under either name, or None."""
    d = deliveries_dir(workspace, recipient)
    for name in (task_id + PENDING_SUFFIX, task_id + CLAIMED_SUFFIX):
        p = d / name
        if p.exists():
            return p
    return None


def claim(sentinel: Path) -> Path:
    """Take a delivery. Atomic and exclusive; a loser gets OSError.

    The loser is the router releasing, or another incarnation of this same
    recipient — never a sibling, which cannot see this folder.
    """
    got = parse_sentinel(sentinel.name)
    if got is None or got[1]:
        raise NotDelivered(f"not an unclaimed sentinel: {sentinel.name}")
    dst = sentinel.with_name(got[0] + CLAIMED_SUFFIX)
    os.rename(sentinel, dst)
    return dst


def release(sentinel: Path) -> Path:
    """Hand a claimed delivery back to the SAME recipient for its next run."""
    got = parse_sentinel(sentinel.name)
    if got is None or not got[1]:
        raise NotDelivered(f"not a claimed sentinel: {sentinel.name}")
    dst = sentinel.with_name(got[0] + PENDING_SUFFIX)
    os.rename(sentinel, dst)
    return dst


def residue(workspace: Path, recipient: str, task_id: str) -> str:
    """What a crash left behind, read without a journal.

    One name per state, each mapped by `sweep` to exactly one action.
    """
    ws = Path(workspace)
    has_result = result_path(ws, task_id).is_file()
    has_flag = done_flag(ws, recipient, task_id).is_file()
    sentinel = find(ws, recipient, task_id)
    payload = payload_path(ws, task_id).is_file()

    # A result outranks everything: the payload is archived last, so between the
    # flag and the archive it still sits in tasks/ and must not read as new work.
    if has_result:
        return "finished" if has_flag else "completed"
    if sentinel is None:
        if payload and not archived_payload(ws, task_id).is_file():
            return "undelivered"
        return "clean"
    if not payload:
        return "stale-sentinel"
    return "died-mid-work" if parse_sentinel(sentinel.name)[1] else "unclaimed"


def sweep(workspace: Path, recipient: str) -> dict:
    """Boot reconciliation. An event that fired while nobody listened is gone,
    so a reader that only streams never learns about it."""
    ws = Path(workspace)
    seen = set()
    actions = {"ready": [], "released": [], "completed": [], "retired": [], "stale": []}
    for p in claimed(ws, recipient) + pending(ws, recipient):
        task_id = parse_sentinel(p.name)[0]
        if task_id in seen:
            continue
        seen.add(task_id)
        state = residue(ws, recipient, task_id)
        if state == "unclaimed":
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
    ap.add_argument("command", choices=("sweep", "pending", "watch", "residue"))
    ap.add_argument("--task-id")
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args(argv)
    ws = Path(a.workspace)

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

    for task_id in sweep(ws, a.recipient)["ready"]:
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
