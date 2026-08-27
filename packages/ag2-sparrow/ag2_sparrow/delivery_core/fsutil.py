"""Shared filesystem primitives for the delivery core.

create_exclusive exists because the check-then-act-on-a-create-if-absent
destination class was found twice in one day (2026-08-17) in independent
codebases: Design B's recover re-arm (CE-4) and #3011's quarantine writer
(exists-then-rename). Any site that must create a file ONLY if absent uses
this primitive instead of re-deriving the pattern — the copy nobody
remembers is the one that ships the bug.

Contract: atomic on POSIX same-fs; exactly one of N racing creators wins;
the loser's bytes are never visible at the destination, not even
transiently; the destination is never truncated or overwritten.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def create_exclusive(path: Path, data: bytes) -> bool:
    """Write `data` to `path` only if `path` does not exist. True = this
    caller created it; False = it already existed (loser: no side effect
    at the destination). Temp is written beside the destination (same fs)
    and linked in — link(2) refuses to clobber, which IS the exclusivity."""
    path = Path(path)
    # mkstemp: O_EXCL under an unpredictable name. A derived name collides
    # in-process and the loser writes through the winner's inode.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                    prefix=f".{path.name}.cx")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
    finally:
        tmp.unlink(missing_ok=True)
