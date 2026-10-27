#!/usr/bin/env python3
"""Readiness of a `results/<task-id>.txt` file, for every delivery consumer.

The single owner of "is this result file ready to send?". Adapters bind their
own resolved results directory and keep only provider-specific delivery; they
must not re-implement the check.

A result path can exist before it holds an answer. In-repo writers publish
whole through `result_publish`, but the core is an LLM driving a shell and will
create the destination for unrelated reasons, and a `>` redirect fills the file
after creating it. File existence is therefore not readiness: a consumer that
treats it as readiness delivers an empty message or a prefix and archives the
task as done, which strands the real answer written moments later (#3956).

A body is ready only when it is non-empty and holds still: unchanged across the
read, and — when younger than `SETTLE_SEC` — unchanged across a short hold too.

A deliberately empty reply is expressed with the `[no-send]` marker, parsed by
`result_markers`, not by writing an empty file.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import NamedTuple

__all__ = ["read_ready_result", "read_ready_result_with_identity", "identity_of",
           "ready_body_of", "is_ready_body", "ResultIdentity", "ReadyResult",
           "SETTLE_SEC"]

# A body younger than this may still be growing under a writer that did not publish whole.
SETTLE_SEC = 1.0
_HOLD_SEC = 0.05
_sleep = time.sleep  # bound here: a drain's loop may swap time.sleep for its own tick sentinel


def _shape(st: os.stat_result) -> tuple:
    return (st.st_size, st.st_mtime_ns)


class ResultIdentity(NamedTuple):
    """The exact publication a consumer read, not a path: inode, the write
    time a rename preserves, and the bytes. A later file at the same name is
    a different reply and must not be disposed of under a decision made about
    this one; an inode the filesystem hands back after an unlink carries a
    new write time, so equal bytes there are still a distinct publication."""
    dev: int
    ino: int
    mtime_ns: int
    digest: str


class ReadyResult(NamedTuple):
    body: str
    identity: ResultIdentity


def identity_of(path: str | Path) -> "tuple[bytes, ResultIdentity]":
    """The bytes at `path` and their identity, from one open: a separate stat
    could describe a file a producer swapped in between."""
    with open(path, "rb") as f:
        st = os.fstat(f.fileno())
        data = f.read()
    return data, ResultIdentity(st.st_dev, st.st_ino, st.st_mtime_ns,
                                hashlib.sha256(data).hexdigest())


def is_ready_body(text: str | None) -> bool:
    """True when `text` is a deliverable body (non-empty after stripping)."""
    return bool(text and text.strip())


def ready_body_of(data: bytes) -> str | None:
    """The deliverable body of bytes already read (one snapshot, e.g. from
    `identity_of`), or None: a partial write mid-character, or blank."""
    try:
        body = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return body or None


def read_ready_result(path: str | Path) -> str | None:
    """Return the stripped body of `path`, or None when it is not ready.

    None covers missing, unreadable, empty-or-whitespace-only and still-being-
    written files. Callers skip on None and retry on a later pass — the file is
    not consumed, so a result that lands between passes is still delivered.
    """
    ready = read_ready_result_with_identity(path)
    return ready.body if ready else None


def read_ready_result_with_identity(path: str | Path) -> "ReadyResult | None":
    """`read_ready_result` plus the identity of the bytes it returned."""
    p = Path(path)
    try:
        before = os.stat(p)
        if before.st_size == 0:
            return None
        data, identity = identity_of(p)
        after = os.stat(p)
        if _shape(after) != _shape(before) or (len(data), identity.mtime_ns) != _shape(after):
            return None  # changed under the read: a prefix, not the body
        if time.time() - after.st_mtime < SETTLE_SEC:
            _sleep(_HOLD_SEC)
            if _shape(os.stat(p)) != _shape(after):
                return None  # young and still growing
    except OSError:
        return None                     # missing or unreadable: readable again on a later pass
    body = ready_body_of(data)
    return ReadyResult(body, identity) if body is not None else None
