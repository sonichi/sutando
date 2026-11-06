#!/usr/bin/env python3
"""The route handler's receipt: proof, on disk, that the watcher consulted it.

Routing happens only when the core's watcher carries the handler in its process
environment, and nothing outside that process can read the environment. The
handler is skill code, so it leaves a receipt every time it is consulted; the
supervisor reads it back and compares it with the tasks the core processed.
A bound room with no receipt, or a task newer than the last receipt, is a host
that is NOT routing — loudly, instead of the core quietly answering for a worker.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

STATE_REL = Path("state") / "pool-routing-receipt.json"


def receipt_path(workspace) -> Path:
    return Path(workspace) / STATE_REL


def record(workspace, *, mode: str, task_id: str, now: float | None = None) -> bool:
    """Stamp the receipt. Never raises: a receipt that could not be written must
    not change a routing decision, so the caller only learns True/False."""
    path = receipt_path(workspace)
    payload = {"consulted_at": time.time() if now is None else float(now),
               "mode": mode, "task_id": task_id}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pool-routing-receipt.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        return False
    return True


def read(workspace) -> dict | None:
    """The last receipt, or None when there has never been one (or it is unreadable —
    a corrupt receipt is no evidence of routing either)."""
    try:
        raw = json.loads(receipt_path(workspace).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("consulted_at"), (int, float)):
        return None
    return raw
