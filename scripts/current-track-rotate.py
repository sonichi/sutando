#!/usr/bin/env python3
"""Rotate a host's current-track.md so the per-pass read stays bounded.

The file is chronological and append-only, and every proactive pass reads it
first, so it grows without bound (222 KB on one host, ~55k tokens, 2026-09-06).
Rotation keeps the pinned preamble (everything before the first dated `## `
entry) plus the newest entries that fit under --keep-bytes, and appends the
older entries, in order, to current-track-archive.md beside it. Nothing is
deleted: head + archive is the original file, and a run under the cap is a no-op.

    current-track-rotate.py <current-track.md> [--keep-bytes 32768] [--dry-run]

Exit 0 rotated or nothing to do; 1 the file is unreadable; 2 bad arguments.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ENTRY = re.compile(r"^(##+ |### )", re.M)
DEFAULT_KEEP = 32 * 1024


def split(text: str) -> tuple[str, list[str]]:
    """(preamble, entries) — entries start at each '## '/'### ' heading."""
    starts = [m.start() for m in ENTRY.finditer(text)]
    if not starts:
        return text, []
    preamble = text[: starts[0]]
    entries = [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]
    return preamble, entries


def plan(text: str, keep_bytes: int) -> tuple[str, str]:
    """(head, archived) — head keeps the preamble and the newest entries under keep_bytes."""
    if len(text.encode("utf-8")) <= keep_bytes:
        return text, ""
    preamble, entries = split(text)
    if not entries:
        return text, ""
    budget = keep_bytes - len(preamble.encode("utf-8"))
    kept: list[str] = []
    for e in reversed(entries):
        size = len(e.encode("utf-8"))
        if kept and size + sum(len(k.encode("utf-8")) for k in kept) > budget:
            break
        kept.insert(0, e)
    archived = entries[: len(entries) - len(kept)]
    return preamble + "".join(kept), "".join(archived)


def rotate(path: Path, keep_bytes: int, dry_run: bool = False) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"current-track-rotate: cannot read {path}: {e}", file=sys.stderr)
        return 1
    head, archived = plan(text, keep_bytes)
    if not archived:
        print(f"current-track-rotate: {path.name} is {len(text.encode())} B <= {keep_bytes} B — nothing to do")
        return 0
    archive = path.with_name(path.stem + "-archive.md")
    if dry_run:
        print(f"current-track-rotate: would move {len(archived.encode())} B to {archive.name}, keep {len(head.encode())} B")
        return 0
    # Archive first, then replace the head: a crash between the two leaves duplicates, never a gap.
    with open(archive, "a", encoding="utf-8") as f:
        f.write(archived)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(head, encoding="utf-8")
    os.replace(tmp, path)
    print(f"current-track-rotate: moved {len(archived.encode())} B to {archive.name}; {path.name} now {len(head.encode())} B")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--keep-bytes", type=int, default=DEFAULT_KEEP)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.keep_bytes <= 0:
        print("current-track-rotate: --keep-bytes must be positive", file=sys.stderr)
        return 2
    return rotate(Path(a.path), a.keep_bytes, a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
