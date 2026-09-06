#!/usr/bin/env python3
"""Rotate a host's current-track.md so the per-pass read stays bounded.

Thin CLI over src/current_track.py, the one writer (append + rotate share a
lock). Nothing is deleted: head + archive is the original.

    current-track-rotate.py <current-track.md> [--keep-bytes 32768] [--dry-run]

Exit 0 rotated or nothing to do; 1 unreadable; 2 bad arguments; 3 the newest
entry alone exceeds the budget — it was kept whole and everything older was
archived, but the head is still over the budget, so say so instead of "nothing
to do".
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from current_track import DEFAULT_KEEP, rotate, split  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--keep-bytes", type=int, default=DEFAULT_KEEP)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.keep_bytes <= 0:
        print("current-track-rotate: --keep-bytes must be positive", file=sys.stderr)
        return 2
    path = Path(a.path)
    try:
        r = rotate(path, a.keep_bytes, a.dry_run)
    except OSError as e:
        print(f"current-track-rotate: cannot read {path}: {e}", file=sys.stderr)
        return 1
    name, arch = path.name, path.stem + "-archive.md"
    if r.oversized:
        _, entries = split(r.head)
        newest = (entries[-1].splitlines() or ["?"])[0][:80] if entries else "the preamble"
        verb = "would keep" if a.dry_run else "kept"
        print(f"current-track-rotate: REFUSING TO CALL THIS BOUNDED — the newest entry alone is "
              f"{len(r.head.encode()) - 0} B of head against a {a.keep_bytes} B budget "
              f"({newest!r}); {verb} it whole, archived {len(r.archived.encode())} B; split or shorten that entry",
              file=sys.stderr)
        return 3
    if not r.archived:
        print(f"current-track-rotate: {name} is {len(r.head.encode())} B <= {a.keep_bytes} B — nothing to do")
        return 0
    if a.dry_run:
        print(f"current-track-rotate: would move {len(r.archived.encode())} B to {arch}, keep {len(r.head.encode())} B")
        return 0
    print(f"current-track-rotate: moved {len(r.archived.encode())} B to {arch}; {name} now {len(r.head.encode())} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
