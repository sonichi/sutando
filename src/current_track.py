#!/usr/bin/env python3
"""The one writer for a host's current-track.md: append and rotate share a lock.

current-track.md is the per-host goal anchor the proactive loop reads first
every pass. It is chronological and append-only, so it grows without bound
(222 KB on one host, 2026-09-06); rotation keeps the pinned preamble plus the
newest entries under a byte budget and moves the older entries, in order, to
current-track-archive.md beside it. Rotation reads the whole file and replaces
it, so an append landing between that read and that replace would be lost:
both operations take the same flock on <file>.lock, and the archive is written
before the head is replaced, so a crash leaves a duplicate, never a gap.

    append(path, text)                -> None
    rotate(path, keep_bytes) -> RotateResult(head, archived, oversized)

An entry that alone exceeds keep_bytes is never truncated: rotate() keeps it,
archives everything older, and reports oversized=True so a caller can refuse
loudly instead of reporting "nothing to do" forever.
"""
from __future__ import annotations

import fcntl
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ENTRY = re.compile(r"^(##+ |### )", re.M)
DEFAULT_KEEP = 32 * 1024


def lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path(path), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def append(path: Path, text: str) -> None:
    """Append under the writer lock; O_APPEND keeps the write a single record."""
    with locked(path):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")


def split(text: str) -> tuple[str, list[str]]:
    """(preamble, entries) — entries start at each '## '/'### ' heading."""
    starts = [m.start() for m in ENTRY.finditer(text)]
    if not starts:
        return text, []
    return text[: starts[0]], [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]


def _size(s: str) -> int:
    return len(s.encode("utf-8"))


@dataclass
class RotateResult:
    head: str
    archived: str
    oversized: bool          # the newest entry alone exceeds the budget; kept, never cut


def plan(text: str, keep_bytes: int) -> RotateResult:
    """Keep the preamble and the newest entries under keep_bytes; the rest is archived."""
    if _size(text) <= keep_bytes:
        return RotateResult(text, "", False)
    preamble, entries = split(text)
    if not entries:
        return RotateResult(text, "", True)
    budget = keep_bytes - _size(preamble)
    kept: list[str] = []
    for e in reversed(entries):
        if kept and _size(e) + sum(_size(k) for k in kept) > budget:
            break
        kept.insert(0, e)
    archived = "".join(entries[: len(entries) - len(kept)])
    head = preamble + "".join(kept)
    return RotateResult(head, archived, _size(head) > keep_bytes)


def rotate(path: Path, keep_bytes: int = DEFAULT_KEEP, dry_run: bool = False,
           _between_read_and_replace=None) -> RotateResult:
    """Rotate under the writer lock. `_between_read_and_replace` is a test seam."""
    with locked(path):
        text = path.read_text(encoding="utf-8")
        r = plan(text, keep_bytes)
        if _between_read_and_replace:
            _between_read_and_replace()
        if dry_run or not r.archived:
            return r
        archive = path.with_name(path.stem + "-archive.md")
        with open(archive, "a", encoding="utf-8") as f:   # archive first: a crash duplicates, never loses
            f.write(r.archived)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(r.head, encoding="utf-8")
        os.replace(tmp, path)
        return r
