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
    replace(path, text)               -> None   (create or rewrite the whole head)
    rotate(path, keep_bytes, pin) -> RotateResult(head, archived, oversized)

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

ENTRY = re.compile(r"^##+ ", re.M)
DEFAULT_KEEP = 32 * 1024

#: Entries other tools read as LIVE STATE, kept at any age: an owner hold that
#: ages out reads as "not held" to every consumer that greps this file.
PIN_DEFAULT = re.compile(
    r"\bHOLD\b|\bhands off\b|\bdo not (?:merge|touch|act|proceed)\b"
    r"|\bin force until\b|\bawait(?:ing)? (?:the )?owner\b|⛔",
    re.I,
)


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


def replace(path: Path, text: str) -> None:
    """Create or rewrite the whole head under the writer lock, atomically (temp + os.replace)."""
    with locked(path):
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        os.replace(tmp, path)


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
    oversized: bool          # head still over budget; nothing was cut to get there
    pinned_bytes: int = 0    # of the head, held by pinned entries — the usual cause
    pinned_count: int = 0


def plan(text: str, keep_bytes: int, pin=PIN_DEFAULT) -> RotateResult:
    """Keep the preamble, every PINNED entry, and the newest of the rest.

    Pinned entries are kept at any age and in their original order. An owner
    hold is an ordinary heading, so age-only rotation moved it to the archive
    and every consumer that greps the head then read "not held" — a silent
    lift, with nothing to notice. `pin=None` restores age-only behaviour.
    """
    if _size(text) <= keep_bytes:
        return RotateResult(text, "", False)
    preamble, entries = split(text)
    if not entries:
        return RotateResult(text, "", True)
    pinned = {i for i, e in enumerate(entries) if pin and pin.search(e)}
    budget = keep_bytes - _size(preamble) - sum(_size(entries[i]) for i in pinned)
    keep = set(pinned)
    used = 0
    for i in range(len(entries) - 1, -1, -1):
        if i in pinned:
            continue
        if keep and used + _size(entries[i]) > budget:
            break
        keep.add(i)
        used += _size(entries[i])
    head = preamble + "".join(entries[i] for i in sorted(keep))
    archived = "".join(entries[i] for i in range(len(entries)) if i not in keep)
    pinned_bytes = sum(_size(entries[i]) for i in pinned)
    return RotateResult(head, archived, _size(head) > keep_bytes, pinned_bytes, len(pinned))


def rotate(path: Path, keep_bytes: int = DEFAULT_KEEP, dry_run: bool = False,
           pin=PIN_DEFAULT, _between_read_and_replace=None) -> RotateResult:
    """Rotate under the writer lock. `_between_read_and_replace` is a test seam."""
    with locked(path):
        text = path.read_text(encoding="utf-8")
        r = plan(text, keep_bytes, pin)
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
