#!/usr/bin/env python3
"""The credential proxy's quota record, read as one fact: did the provider
allow requests at its last observation, and how long ago was that.

The proxy writes `state/quota-state.json` from the unified rate-limit headers
on every real request. That makes it the provider's own statement about the
account, refreshed by traffic, and the right thing to consult before believing
a limit banner on a pane -- a pane is a screenshot of the last thing that ran.

Dependency-light on purpose: `pane_gate` (the delivery verdict every notifier
shares) reads it on the hot path.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from workspace_default import status_read_path

#: A limit can begin at any moment, so only a RECENT observation vouches for now.
#: Six hours (health-check's "is the proxy wired" horizon) is the wrong horizon here.
FRESH_SEC = 10 * 60

_STATUS_KEYS = (
    "anthropic-ratelimit-unified-status",
    "anthropic-ratelimit-unified-5h-status",
    "anthropic-ratelimit-unified-7d-status",
)


@dataclass(frozen=True)
class QuotaRecord:
    allowed: Optional[bool]   # None: the record does not say either way
    age_s: Optional[float]    # None: no usable timestamp on the record

    def fresh(self, fresh_sec: float = FRESH_SEC) -> bool:
        return self.age_s is not None and 0 <= self.age_s <= fresh_sec


def _parse_when(value) -> Optional[float]:
    """An ISO-8601 timestamp (the proxy writes `...Z`) as epoch seconds, else None."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def read_quota_record(workspace, now: Optional[float] = None) -> Optional[QuotaRecord]:
    """The record at `<workspace>/state/quota-state.json`, or None when it is
    absent or unreadable. Never raises: a caller on a delivery path fails closed."""
    path = status_read_path("quota-state.json", Path(workspace))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    headers = data.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    statuses = [headers[k] for k in _STATUS_KEYS if k in headers]
    if statuses:
        allowed: Optional[bool] = all(s == "allowed" for s in statuses)
    elif isinstance(data.get("available"), bool):
        allowed = data["available"]
    else:
        allowed = None
    when = _parse_when(data.get("last_checked"))
    if when is None:
        try:
            when = path.stat().st_mtime
        except OSError:
            when = None
    at = time.time() if now is None else now
    return QuotaRecord(allowed, None if when is None else at - when)


def provider_allows_now(workspace, now: Optional[float] = None,
                        fresh_sec: float = FRESH_SEC) -> bool:
    """True only when a FRESH record says allowed. Absent, stale, unreadable and
    rejected all answer False: silence never overrides a banner, only the provider's word does."""
    rec = read_quota_record(workspace, now)
    return bool(rec and rec.allowed is True and rec.fresh(fresh_sec))


if __name__ == "__main__":
    import sys
    ws = sys.argv[1] if len(sys.argv) > 1 else None
    if ws is None:
        from workspace_default import resolve_workspace
        ws = resolve_workspace()
    rec = read_quota_record(ws)
    if rec is None:
        print("quota-record: absent or unreadable")
        raise SystemExit(2)
    age = "unknown age" if rec.age_s is None else f"{int(rec.age_s)}s old"
    print(f"quota-record: allowed={rec.allowed} {age} fresh={rec.fresh()}")
    raise SystemExit(0 if provider_allows_now(ws) else 1)
