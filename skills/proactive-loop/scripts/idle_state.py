#!/usr/bin/env python3
"""The one writer contract for `state/idle-streak.json`.

Two tools mutate this record — `idle-surface-hash.py` (counters, hash) and
`idle-held.py` (the held list, notes). A read-modify-replace that skips the
sidecar lock silently drops whichever concurrent write lands first, and the
losing writer still reports success, so the contract lives here rather than
being restated per call site.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

# Returned by a mutate() that declines to write. None is NOT this sentinel, so
# a mutate that forgets to return cannot silently skip its own write.
ABORT = object()


def read_state(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, doc: dict, indent: int | None = None) -> None:
    """Per-PID staging: several loop processes may publish this file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=indent,
                              sort_keys=indent is not None))
    os.replace(tmp, path)


def locked_update(path: Path, mutate, indent: int | None = None):
    """Read, `mutate(doc)`, write — all inside the record's exclusive lock.

    The read must be INSIDE it: a doc read earlier is already stale.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".json.lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            doc = read_state(path)
            result = mutate(doc)
            if result is ABORT:
                return ABORT
            write_state(path, doc, indent=indent)
            return result
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
