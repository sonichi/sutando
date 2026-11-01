"""The Discord bridge's record of when it opened new gateway sessions: one writer, the bridge.

health-check reads it to spot a bridge that keeps opening fresh sessions, which a heartbeat does not
show. A file the bridge alone writes, not its log, which also carries message bodies.
Schema: {"sessions": [epoch seconds, ...]}, oldest first, at most KEPT entries within WINDOW_S.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

WINDOW_S = 7200
KEPT = 500
CLOCK_SKEW_S = 60


def _read(path: Path) -> list[float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [float(t) for t in raw
            if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)]


def record(path: Path, now: float) -> None:
    """Add one new session at `now`, keeping the window; atomic, and never raises."""
    try:
        kept = [t for t in _read(path) if now - WINDOW_S <= t <= now + CLOCK_SKEW_S]
        kept = (kept + [now])[-KEPT:]
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"sessions": kept}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def count_recent(path: Path, now: float, window_s: float = 3600) -> int:
    """New sessions within the last `window_s`; a time ahead of `now` is not counted."""
    return sum(1 for t in _read(path) if now - window_s <= t <= now + CLOCK_SKEW_S)
