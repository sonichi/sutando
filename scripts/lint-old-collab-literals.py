#!/usr/bin/env python3
"""Refuse NEW occurrences of the retired `room-doc` spellings.

The rename keeps a few old spellings alive on purpose for one release — the
forwarder, the env aliases, the ALB path until the service moves, a stored
state path. A grep-to-zero gate would therefore fail today and teach nothing.
This one holds a baseline of file -> count and fails when any file grows or a
new file appears; phase C shrinks the baseline to nothing and the gate becomes
grep-to-zero by itself.

Run: python3 scripts/lint-old-collab-literals.py [--update]   (exit 0 ok / 1 fail)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "scripts" / "old-collab-literals.baseline.json"
PATTERN = re.compile(r"room-doc|room_doc|space\.ag2\.doc\b")
# Text files only; the baseline itself and this script are not evidence.
SKIP = {str(BASELINE.relative_to(REPO)), "scripts/lint-old-collab-literals.py"}


def counts() -> dict[str, int]:
    files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True).stdout.split("\n")
    out: dict[str, int] = {}
    for rel in files:
        if not rel or rel in SKIP:
            continue
        p = REPO / rel
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        n = len(PATTERN.findall(text))
        if n:
            out[rel] = n
    return out


def main(argv: list[str]) -> int:
    now = counts()
    if "--update" in argv:
        BASELINE.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline written: {len(now)} files, {sum(now.values())} occurrences")
        return 0
    base = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    grew = {f: (base.get(f, 0), n) for f, n in now.items() if n > base.get(f, 0)}
    if grew:
        print("old-collab-literals: FAIL — retired spellings grew (baseline -> now):")
        for f, (b, n) in sorted(grew.items()):
            print(f"  {f}: {b} -> {n}")
        print("Use the collab spelling; if an alias is deliberate, say why in the PR and "
              "run with --update in the same commit.")
        return 1
    shrunk = sum(base.values()) - sum(min(n, base.get(f, 0)) for f, n in now.items())
    print(f"old-collab-literals: ok ({sum(now.values())} occurrences in {len(now)} files"
          + (f", {shrunk} fewer than the baseline — run --update to lock the gain)" if shrunk else ")"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
