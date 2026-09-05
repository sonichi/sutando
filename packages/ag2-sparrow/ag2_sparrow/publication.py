#!/usr/bin/env python3
"""Publication of a `results/` file, for every proactive producer.

The single owner of "make this body visible to consumers". Readiness
(`readiness.py`) is the consumer half of the same contract: it can only decide
that a body is complete if the producer never publishes an incomplete one.

A poller claims `results/proactive-*.txt` on sight — it hard-links the inode and
unlinks the name. A body written in place is therefore claimable, and retirable,
while it is still being written: the consumer sends the prefix it happened to
read and destroys the name the rest would have arrived at. Publishing through a
rename removes the window rather than narrowing it.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["publish_result"]


def publish_result(path: str | Path, body: str) -> Path:
    """Write `body` so `path` appears whole or not at all; return the path.

    The scratch name is dotted and in the destination directory, so no consumer
    glob matches it and the rename cannot cross a filesystem.
    """
    p = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return p
