"""Atomic publication of the owner's most recent messaging activity.

The direct-message adapters all update one provider-neutral workspace record.
This module owns its schema, summary bound, and collision-safe publication; an
adapter supplies only the resolved destination and its provider-specific logger.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Callable, Optional


def write_owner_activity(
    path: Path,
    channel: str,
    summary: str,
    channel_id=None,
    *,
    on_error: Optional[Callable[[Exception], None]] = None,
) -> bool:
    """Publish one ``last-owner-activity.json`` record atomically.

    The per-invocation PID + UUID staging name is unique across processes and
    threads. Publication is best-effort: failures are reported through
    ``on_error`` and returned as ``False`` instead of interrupting message
    intake.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": int(time.time()),
            "channel": channel,
            "summary": summary[:80],
        }
        if channel_id:
            payload["channel_id"] = str(channel_id)
        tmp = path.with_suffix(
            f".json.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
        return True
    except Exception as exc:
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                pass
        return False
