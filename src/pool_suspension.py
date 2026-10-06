"""Shared reader for the worker pool's suspension marker, `state/pool-suspended`.

The host that owns the pool writes this marker when it deliberately stops (an app quit)
and removes it on resume. Three readers interpret it: the pool's own remedy, the local
health snapshot and health-check. Each renders it its own way; what lives here is the
part they must agree on: whether the pool is suspended, and the normalised record.

A marker that is not the JSON record still suspends; it just names no workers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402

REL = Path("state") / "pool-suspended"
DEFAULT_REASON = "suspended"


def path(workspace=None) -> Path:
    """The marker's path; `workspace=None` resolves through the one sanctioned helper."""
    return Path(workspace if workspace is not None else resolve_workspace()) / REL


def normalise(text: str) -> dict:
    """The record {reason, at, stopped} from the marker's text, whatever its shape."""
    try:
        rec = json.loads(text)
    except ValueError:
        rec = None
    if not isinstance(rec, dict):
        return {"reason": text or DEFAULT_REASON, "at": None, "stopped": []}
    reason, at, stopped = rec.get("reason"), rec.get("at"), rec.get("stopped")
    return {
        "reason": reason if isinstance(reason, str) and reason else DEFAULT_REASON,
        "at": at if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
        "stopped": [w for w in stopped if isinstance(w, str)] if isinstance(stopped, list) else [],
    }


def read(workspace=None) -> dict | None:
    """The normalised record, or None when the pool is not suspended. A marker that
    exists but cannot be read raises OSError: each reader decides what that means."""
    try:
        text = path(workspace).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return normalise(text)
