#!/usr/bin/env python3
"""Launch hook: put the shipped voice walkthrough at <workspace>/notes/first-time-tutorial.md once.

The voice mode reads that file, and nothing else writes it. The copy is made only when the file is
missing, so an owner's own edits are never replaced; a later run with the file present does nothing.
Declared as the skill's `on_launch` (skills/MANIFEST.md); it bounds its own work and always exits 0.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SOURCE = HERE.parent / "voice" / "first-time-tutorial.md"
SOURCE_MAX = 64 * 1024


def target(workspace: Path) -> Path:
    return workspace / "notes" / "first-time-tutorial.md"


def seed(workspace: Path, source: Path = SOURCE) -> str:
    """'seeded', 'present' or 'skipped: <why>'; never overwrites a file that exists."""
    dest = target(workspace)
    if os.path.lexists(dest):
        return "present"
    try:
        data = source.read_bytes()
    except OSError as exc:
        return f"skipped: {type(exc).__name__}"
    if not data or len(data) > SOURCE_MAX:
        return "skipped: source empty or too large"
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".first-time-tutorial.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        # link, not replace: a file created meanwhile is kept, never overwritten.
        os.link(tmp, dest)
    except FileExistsError:
        return "present"
    except OSError:
        return _create_exclusive(dest, data)  # a filesystem without hard links
    finally:
        Path(tmp).unlink(missing_ok=True)
    return "seeded"


def _create_exclusive(dest: Path, data: bytes) -> str:
    """Create `dest` only if absent (O_EXCL), so a file made at any moment before is kept;
    a write that fails removes the file it created, leaving the next launch to try again."""
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return "present"
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        dest.unlink(missing_ok=True)
        return f"skipped: {type(exc).__name__}"
    return "seeded"


def _workspace() -> Path:
    sys.path.insert(0, str(REPO / "src"))
    from workspace_default import resolve_workspace
    return resolve_workspace(migrate=False)


def main() -> int:
    try:
        print(f"seed_voice_tutorial: {seed(_workspace())}")
    except Exception as exc:  # noqa: BLE001 — a launch hook never fails the launch
        print(f"seed_voice_tutorial: skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
