#!/usr/bin/env python3
"""Readiness of a `results/<task-id>.txt` file, for every delivery consumer.

The single owner of "is this result file ready to send?". Adapters bind their
own resolved results directory and keep only provider-specific delivery; they
must not re-implement the check.

A result path can exist before it holds an answer. The core writes
temp-file-then-rename, but it is an LLM driving a shell and will create the
destination for unrelated reasons, and a partial write can be observed
mid-content. File existence is therefore not readiness: a consumer that treats
it as readiness delivers an empty message and archives the task as done, which
strands the real answer written moments later.

A deliberately empty reply is expressed with the `[no-send]` marker, parsed by
`result_markers`, not by writing an empty file.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import NamedTuple

__all__ = ["read_ready_result", "read_ready_result_with_identity", "identity_of",
           "ready_body_of", "is_ready_body", "ResultIdentity", "ReadyResult"]


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

    None covers missing, unreadable and empty-or-whitespace-only files. Callers
    skip on None and retry on a later pass — the file is not consumed, so a
    result that lands between passes is still delivered.
    """
    ready = read_ready_result_with_identity(path)
    return ready.body if ready else None


def read_ready_result_with_identity(path: str | Path) -> "ReadyResult | None":
    """`read_ready_result` plus the identity of the bytes it returned."""
    try:
        data, identity = identity_of(Path(path))
    except OSError:
        return None                     # missing or unreadable: readable again on a later pass
    body = ready_body_of(data)
    return ReadyResult(body, identity) if body is not None else None
