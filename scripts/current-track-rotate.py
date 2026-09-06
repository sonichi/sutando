#!/usr/bin/env python3
"""Rotate a host's current-track.md so the per-pass read stays bounded.

Thin CLI over src/current_track.py, the one writer (append + rotate share a
lock). Nothing is deleted: head + archive is the original.

    current-track-rotate.py <current-track.md> [--keep-bytes 32768] [--dry-run]

Entries other tools read as live state (an owner hold) stay in the head at any
age; `--pin REGEX` replaces that vocabulary and `--no-pin` disables it.

Exit 0 rotated or nothing to do; 1 unreadable; 2 bad arguments; 3 the newest
entry alone exceeds the budget — it was kept whole and everything older was
archived, but the head is still over the budget, so say so instead of "nothing
to do".
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from current_track import DEFAULT_KEEP, PIN_DEFAULT, rotate, split  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--keep-bytes", type=int, default=DEFAULT_KEEP)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pin", metavar="REGEX",
                    help="entries matching this stay in the head at any age "
                         "(default: the owner-hold vocabulary)")
    ap.add_argument("--no-pin", action="store_true",
                    help="age-only rotation; a hold in an old entry WILL be archived")
    a = ap.parse_args(argv)
    if a.keep_bytes <= 0:
        print("current-track-rotate: --keep-bytes must be positive", file=sys.stderr)
        return 2
    if a.pin and a.no_pin:
        print("current-track-rotate: --pin and --no-pin are exclusive", file=sys.stderr)
        return 2
    try:
        pin = None if a.no_pin else (re.compile(a.pin, re.I) if a.pin else PIN_DEFAULT)
    except re.error as e:
        print(f"current-track-rotate: --pin is not a regex: {e}", file=sys.stderr)
        return 2
    path = Path(a.path)
    try:
        r = rotate(path, a.keep_bytes, a.dry_run, pin)
    except OSError as e:
        print(f"current-track-rotate: cannot read {path}: {e}", file=sys.stderr)
        return 1
    name, arch = path.name, path.stem + "-archive.md"
    if r.oversized:
        head_b, keep_b = len(r.head.encode()), a.keep_bytes
        # Pins are the cause when the head would fit without them.
        if r.pinned_count and head_b - r.pinned_bytes <= keep_b:
            did = ("would archive" if a.dry_run else "ARCHIVED")
            print(f"current-track-rotate: ROTATED BUT STILL OVER BUDGET — {did} "
                  f"{len(r.archived.encode())} B; the head is {head_b} B against a {keep_b} B "
                  f"budget because {r.pinned_count} pinned "
                  f"entr{'y' if r.pinned_count == 1 else 'ies'} (owner holds and the like) hold "
                  f"{r.pinned_bytes} B, which rotation may not move. Raise --keep-bytes, or "
                  f"retire the holds that have expired", file=sys.stderr)
        else:
            _, entries = split(r.head)
            newest = (entries[-1].splitlines() or ["?"])[0][:80] if entries else "the preamble"
            verb = "would keep" if a.dry_run else "kept"
            print(f"current-track-rotate: ROTATED BUT STILL OVER BUDGET — the newest entry alone is "
                  f"{head_b} B of head against a {keep_b} B budget ({newest!r}); {verb} it whole, "
                  f"archived {len(r.archived.encode())} B; split or shorten that entry",
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
