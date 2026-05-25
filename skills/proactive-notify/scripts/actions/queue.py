"""Queue action — appends the ping to a local JSONL file for later review.

Used for fyi-tier pings and quiet-hours downgrades.  A morning-briefing or
drain-queue script can consume and clear the file.

State: $WORKSPACE/skills/proactive-notify/state/queue.jsonl
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

_QUEUE_FILE = resolve_workspace() / "skills" / "proactive-notify" / "state" / "queue.jsonl"


def send(ping, body: str) -> dict:
    """Append entry to the queue file.  Always succeeds unless the FS is full."""
    ts = datetime.now(timezone.utc)
    entry_id = f"q-{ts.strftime('%Y%m%dT%H%M%SZ')}"
    record = {
        "id": entry_id,
        "ts": ts.isoformat(),
        "ping": str(getattr(ping, "name", "") or ""),
        "urgency": str(getattr(ping, "urgency", "fyi") or "fyi"),
        "body": body,
    }
    try:
        _QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _QUEUE_FILE.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return {"ok": True, "id": entry_id}
    except OSError as e:
        return {"ok": False, "error": str(e)}
