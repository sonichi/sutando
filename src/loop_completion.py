"""Host-specific proof that a proactive-loop pass completed."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Optional

from file_lock import locked_file
from workspace_default import write_status


def completion_path(workspace: Path, host: str) -> Path:
    if not isinstance(host, str) or not host or len(host.encode()) > 180 or host in (".", "..") or any(c in host for c in ("/", "\\", "\x00")):
        raise ValueError("a single host label is required")
    return Path(workspace) / "state" / "loop-completions" / (host + ".json")


def _timestamp(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("completion time must be finite")
    try:
        ts = float(value)
    except OverflowError as exc:
        raise ValueError("completion time must be finite") from exc
    if not math.isfinite(ts) or ts < 0:
        raise ValueError("completion time must be finite")
    return ts


def read_completion(workspace: Path, host: str) -> Optional[float]:
    try:
        value = json.loads(completion_path(workspace, host).read_text())
        ts = _timestamp(value.get("completed_at"))
        if type(value.get("version")) is not int or value["version"] != 1 or value.get("host") != host:
            return None
        return ts
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def write_completion(workspace: Path, host: str, *, now: Optional[float] = None) -> Path:
    ts = _timestamp(time.time() if now is None else now)
    target = completion_path(workspace, host)
    target.parent.mkdir(parents=True, exist_ok=True)
    with locked_file(target.with_suffix(".lock")):
        previous = read_completion(workspace, host)
        if previous is None or ts >= previous:
            write_status(
                str(target.relative_to(Path(workspace) / "state")),
                {"version": 1, "host": host, "completed_at": ts},
                workspace=workspace,
            )
    return target
