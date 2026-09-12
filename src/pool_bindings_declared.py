#!/usr/bin/env python3
"""Does <state>/bindings.json declare a worker binding? A yes/no the task
watcher asks before starting without a routing handler.

Exit 0: none (absent file, empty declaration, or every room bound to the core).
Exit 1: declared, or unreadable / mis-shaped — fail closed: a corrupt file is
"workers named", not "no workers", because the loader that compiles the
roster refuses it the same way. One line of reason on stdout either way.
The envelope is the pool skill's data (`{"bindings": {room: worker}}`); this
reads it as a file and imports nothing from the skill.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CORE = "core"


def declared(state_dir: str | Path) -> tuple[bool, str]:
    p = Path(state_dir) / "bindings.json"
    if not p.exists():
        return False, f"no bindings declared ({p} absent)"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return True, f"bindings unreadable, treated as declared: {p}: {e}"
    bindings = raw.get("bindings") if isinstance(raw, dict) else None
    if not isinstance(bindings, dict):
        return True, f"bindings mis-shaped, treated as declared: {p}"
    to_workers = {}
    for room, bound in bindings.items():
        members = bound if isinstance(bound, list) else [bound]
        if any(m != CORE for m in members):
            to_workers[room] = bound
    if not to_workers:
        return False, f"no worker bindings declared in {p}"
    return True, f"{len(to_workers)} room(s) bound to workers in {p}"


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: pool_bindings_declared.py <state-dir>", file=sys.stderr)
        return 2
    yes, reason = declared(args[0])
    print(reason)
    return 1 if yes else 0


if __name__ == "__main__":
    sys.exit(main())
