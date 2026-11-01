"""The session's own activity rows: which task a row the session's hooks wrote names, and when.

One reader for every consumer (the bridge's stall watch, the Stop interrupt), vendored into
ag2-sparrow by tools/sync_from_src.py so the two can never read the rows differently.
"""
from __future__ import annotations


def session_row(rec) -> "tuple[str, float, str] | None":
    """(task id, ts, kind) of a row the session's own hooks wrote, else None."""
    # Delivery (TASK_STATUS) rows and owner rows are written for the task, not by its turn.
    if not isinstance(rec, dict) or rec.get("projection") or rec.get("audience"):
        return None
    task, ts = rec.get("task"), rec.get("ts")
    if not isinstance(task, dict) or not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    return task.get("id"), ts, rec.get("kind")
