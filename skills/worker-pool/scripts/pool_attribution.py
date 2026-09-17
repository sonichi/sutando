#!/usr/bin/env python3
"""Assignment-time provenance: which worker a task was given to.

Attribution is established when the router assigns the task and is immutable
afterwards. Result producers never self-assert it: a worker claiming its own
identity is a claim by the thing being identified, while this is a fact
recorded by the component that made the routing decision.

Path convention (`state/attribution/<task_id>`) is READ BY THE BRIDGE, which
is a standalone package that cannot import this optional skill — the same
arrangement `done_flag()` already has with `_worker_of()`. The two are kept in
step by a test that builds its fixtures through `attribution_path()` here, so a
drift fails a test instead of silently losing attribution.

One line, one worker id: a task the router fans out to several recipients has
no single author, so the first write wins and later ones are refused rather
than overwriting — an ambiguous attribution must not silently become the last
writer's.
"""
from __future__ import annotations

from pathlib import Path

_WORKER_ID_CHARS = set("0123456789abcdef")


def _root(workspace) -> Path:
    return Path(workspace)


def attribution_dir(workspace) -> Path:
    return _root(workspace) / "state" / "attribution"


def attribution_path(workspace, task_id: str) -> Path:
    """The one file naming this task's assigned worker."""
    return attribution_dir(workspace) / task_id


def is_worker_id(value: str) -> bool:
    """The pool's instance-id grammar. The core is not a worker and must never
    be recorded here — a core-handled task has no worker attribution, and
    writing one would make the bridge stamp a worker that never ran."""
    return (
        isinstance(value, str)
        and len(value) == 32
        and set(value) <= _WORKER_ID_CHARS
    )


def record(workspace, task_id: str, worker_id: str) -> bool:
    """Persist `task_id -> worker_id` at assignment. Returns True if this call
    wrote it, False if an attribution already existed (first write wins).

    O_EXCL, so two routers racing the same task cannot both claim it.
    """
    if not task_id or not is_worker_id(worker_id):
        return False
    d = attribution_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    p = d / task_id
    try:
        with open(p, "x", encoding="utf-8") as fh:
            fh.write(worker_id)
    except FileExistsError:
        return False
    return True


def worker_for_task(workspace, task_id: str) -> str | None:
    """The assigned worker, or None when this task has no worker attribution.

    None means "not attributed", never "attributed to the core": the caller
    decides what an unattributed result means, and for a task that should have
    belonged to a pool worker that is an anomaly to surface, not a default.
    """
    if not task_id:
        return None
    try:
        value = attribution_path(workspace, task_id).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
        return None
    return value if is_worker_id(value) else None
