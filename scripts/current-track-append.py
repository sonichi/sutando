#!/usr/bin/env python3
"""Append an entry to a host's current-track.md under the writer lock.

    printf '## 2026-09-06T02:00Z — …\n' | current-track-append.py <current-track.md>

Shares src/current_track.py's lock with rotation, so an entry can never land
between rotation's read and its replace. Exit 0 appended; 1 empty stdin.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from current_track import append  # noqa: E402


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: current-track-append.py <current-track.md>  (entry on stdin)", file=sys.stderr)
        return 2
    text = sys.stdin.read()
    if not text.strip():
        print("current-track-append: empty entry, nothing written", file=sys.stderr)
        return 1
    append(Path(argv[0]), text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
