"""Give pool workers their own enabled s.space agent in a test workspace's connections record.

The router admits new work only for a worker whose `connections.py` row is enabled, so a fixture
that routes to a worker writes that row first: `give_identities(ws, "w1", "w2")`. The record is
written whole (one core row plus one row per worker); pass `disabled`/`parked` to write such rows.
"""
from __future__ import annotations

import json
from pathlib import Path

SERVER = "s.space"


def _row(instance: str, n: int, **kw) -> dict:
    row = {"server": SERVER, "instance": instance, "gateway_url": "https://chat.s.space/relay",
           "token_ref": f"AG2_REMOTE_TOKEN_{instance.upper().replace('-', '_')}", "primary": n == 0,
           "added_by": "owner", "added_at": "2026-11-01T00:00:00Z", "verified": True,
           "server_source": "identity", "agent_mxid": f"@agent{n}:{SERVER}"}
    row.update(kw)
    return row


def give_identities(workspace, *workers: str, **flags) -> Path:
    """Write the record naming each of `workers` with its own row; returns its path."""
    rows = [_row("s-space", 0)]
    rows += [_row(f"s-space-w{i}", i, worker=w, **flags) for i, w in enumerate(workers, 1)]
    path = Path(workspace) / "state" / "connections.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "connections": rows}), encoding="utf-8")
    return path
