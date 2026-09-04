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
import sys
from pathlib import Path

# Returned by a mutate() that declines to write. None is NOT this sentinel, so
# a mutate that forgets to return cannot silently skip its own write.
ABORT = object()


# Returned by locked_update when the record exists but cannot be trusted. An
# absent file is NOT this: absent means first run, and {} is the right answer.
REFUSED = object()


def read_state(path: Path) -> dict:
    """Lenient read for callers that want a default. NEVER use before a write."""
    try:
        doc = json.loads(Path(path).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def read_state_strict(path: Path):
    """(doc, err) — absent is ({}, None); unreadable or malformed is (None, why).

    Collapsing those two into {} makes a truncated file look like a fresh one.
    """
    p = Path(path)
    if not p.exists():
        return {}, None
    try:
        raw = p.read_text()
    except OSError as exc:
        return None, f"unreadable ({exc.__class__.__name__})"
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        return None, f"not JSON ({exc})"
    if not isinstance(doc, dict):
        return None, f"not a JSON object (got {type(doc).__name__})"
    return doc, None


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
            doc, err = read_state_strict(path)
            if err is not None:
                # A parse failure is not authorisation to discard the record.
                print(f"REFUSED: {path} {err} — not overwriting", file=sys.stderr)
                return REFUSED
            result = mutate(doc)
            if result is ABORT:
                return ABORT
            write_state(path, doc, indent=indent)
            return result
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
