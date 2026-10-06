#!/usr/bin/env python3
"""Write a host's current-track.md under the writer lock: append an entry, or replace the whole head.

    printf '## 2026-09-06T02:00Z — …\n' | current-track-write.py append  <current-track.md>
    cat new-head.md                       | current-track-write.py replace <current-track.md>

Both share src/current_track.py's lock with rotation, so neither an entry nor a rewrite can land
between rotation's read and its replace. `replace` is the "create it if absent / rewrite it when the
track moves" path the context-reconstruct skill prescribes; `append` is the per-pass entry.
A write that rotates says so on stderr. `append` rotates in its own lock when the entry crosses
the read budget, and a silent rotation is how a later pass reads a head whose older entries moved
without anyone deciding that; the pin-only `oversized` case is reported too, since nothing was cut.
Exit 0 written; 1 empty stdin; 2 usage.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from current_track import DEFAULT_KEEP, _size, append, replace  # noqa: E402

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
    rotated = OPS[argv[0]](Path(argv[1]), text)
    if rotated is not None:
        # A rotation is the caller's business: it decides which entries a later pass can still read.
        # `oversized` fires with or without pins, so the pinned clause is only printed when it explains.
        where = "still over budget" if rotated.oversized else "rotated"
        why = ""
        if rotated.pinned_count:
            why = (f", {rotated.pinned_count} pinned entr"
                   f"{'y' if rotated.pinned_count == 1 else 'ies'} holding {rotated.pinned_bytes} B")
        # _size, not len: the budget and `oversized` are UTF-8 bytes, and these entries carry em-dashes.
        print(f"current-track-write: {where} — archived {_size(rotated.archived)} B, head now "
              f"{_size(rotated.head)} B of a {DEFAULT_KEEP} B budget{why}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
