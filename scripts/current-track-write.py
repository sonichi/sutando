#!/usr/bin/env python3
"""Write a host's current-track.md under the writer lock: append an entry, or replace the whole head.

    printf '## 2026-09-06T02:00Z — …\n' | current-track-write.py append  <current-track.md>
    cat new-head.md                       | current-track-write.py replace <current-track.md>

Both share src/current_track.py's lock with rotation, so neither an entry nor a rewrite can land
between rotation's read and its replace. `replace` is the "create it if absent / rewrite it when the
track moves" path the context-reconstruct skill prescribes; `append` is the per-pass entry.
Exit 0 written; 1 empty stdin; 2 usage.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from current_track import append, replace  # noqa: E402

OPS = {"append": append, "replace": replace}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] not in OPS:
        print("usage: current-track-write.py append|replace <current-track.md>  (text on stdin)", file=sys.stderr)
        return 2
    text = sys.stdin.read()
    if not text.strip():
        print(f"current-track-write: empty stdin, nothing written ({argv[0]})", file=sys.stderr)
        return 1
    OPS[argv[0]](Path(argv[1]), text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
