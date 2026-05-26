#!/usr/bin/env python3
"""
CI gate: ban process.cwd() / Path.cwd() / os.getcwd() in src/ outside the
canonical workspace resolvers (closes #863).

Context: The 2026-05-18 audit closed ~28 sites where workspace data was
resolved against cwd() instead of resolveWorkspace() / resolve_workspace().
Each future caller that writes workspace data via cwd-relative paths
re-introduces the same bug class. This script is the regression guard.

FLAGGED (in src/ only):
  TypeScript/JS:  process.cwd()         — outside workspace_default.ts, util_paths.ts
  Python:         Path.cwd()            — outside workspace_default.py, util_paths.py
                  os.getcwd()           — same allowlist

NOT flagged:
  - Lines that are pure comments (// or # after leading whitespace).
  - Files in the allowlist (the canonical resolvers).
  - Anything under skills/ — skills use resolveWorkspace() but some have
    legitimate REPO_ROOT lookups that may call cwd() transitionally.

Exit 1 on any hit, 0 otherwise. Run from the repo root.
"""

import re
import sys
from pathlib import Path

SCAN_DIR = Path(__file__).resolve().parent.parent / "src"

# Files that ARE the canonical resolvers — they're allowed to call cwd().
ALLOWLIST = {
    "workspace_default.ts",
    "workspace_default.py",
    "util_paths.ts",
    "util_paths.py",
}

# Patterns to flag.
_CWD_CALL_RE = re.compile(r"process\.cwd\(\)|Path\.cwd\(\)|os\.getcwd\(\)")

# Comment prefixes — lines that start with these (after stripping) are skipped.
_COMMENT_RE = re.compile(r"^\s*(?://|#)")


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, stripped_line)] for each offending line in path."""
    hits: list[tuple[int, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return hits
    for i, line in enumerate(lines, start=1):
        if _COMMENT_RE.match(line):
            continue
        if _CWD_CALL_RE.search(line):
            hits.append((i, line.strip()))
    return hits


def main() -> int:
    if not SCAN_DIR.is_dir():
        print(f"check-no-cwd-writes: {SCAN_DIR} not found — nothing to scan.")
        return 0

    # Scan TypeScript source and Python source only — not compiled .js outputs.
    extensions = (".ts", ".py")
    offenders: list[str] = []

    for path in sorted(SCAN_DIR.rglob("*")):
        if path.suffix not in extensions:
            continue
        if path.name in ALLOWLIST:
            continue
        for ln, src in scan_file(path):
            rel = path.relative_to(SCAN_DIR.parent)
            offenders.append(f"{rel}:{ln}: {src}")

    if offenders:
        print("cwd() lint FAILED — process.cwd()/Path.cwd()/os.getcwd() outside canonical resolvers in src/:")
        for o in offenders:
            print(f"  {o}")
        print()
        print("Fix: use resolveWorkspace() (TS) or resolve_workspace() (Python) instead.")
        print("Allowlisted: workspace_default.ts/py, util_paths.ts/py")
        return 1

    print("cwd() lint passed — no cwd() writes outside canonical resolvers in src/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
