#!/usr/bin/env python3
"""Read-only agent discovery over the per-host liveness directory.

Backs the runtime-API `agent.list` / `agent.status` methods: enumerate the
cores this workspace knows about from `state/cores/<host>.alive` heartbeats
(written by core_heartbeat.py) without touching any process, socket, or tmux
detail — callers see identity + liveness, never the execution backend.

Liveness is the heartbeat file's mtime (younger than ALIVE_MAX_AGE_S → alive);
a graceful shutdown unlinks the file, so absence means offline. The payload is
passed through as self-reported metadata — mtime stays the only trust signal.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from state_records import read_record

# Matches the documented cores-heartbeat contract: beats every 30s, readers
# treat mtime younger than ~90s as alive.
ALIVE_MAX_AGE_S = 90.0


class AgentsView:
    """Pure view over <state>/cores/. Constructed with the state dir so the
    dispatcher stays free of path resolution (server composes the real path,
    tests hand in a tmp dir)."""

    def __init__(self, state_dir: str | Path):
        self.cores_dir = Path(state_dir) / "cores"

    def list_agents(self) -> dict:
        agents = []
        if self.cores_dir.is_dir():
            for f in sorted(self.cores_dir.glob("*.alive")):
                agents.append(self._entry(f))
        return {"agents": agents}

    def agent_status(self, agent_id: str) -> dict | None:
        """Status for one agent, or None if no heartbeat file matches.
        `agent_id` matches the heartbeat basename (the per-host label) or the
        payload's self-reported host."""
        if not agent_id:
            return None
        if self.cores_dir.is_dir():
            for f in self.cores_dir.glob("*.alive"):
                entry = self._entry(f)
                if agent_id in (f.stem, entry.get("host")):
                    return entry
        return None

    def _entry(self, f: Path) -> dict:
        try:
            age = time.time() - f.stat().st_mtime
        except OSError:  # unlinked between glob and stat — offline
            return {"agentId": f.stem, "alive": False}
        payload = read_record(f) or {}
        out = {
            "agentId": f.stem,
            "alive": 0 <= age < ALIVE_MAX_AGE_S,
            "beatAgeS": round(age, 1),
        }
        for k in ("host", "pid", "status", "socket", "locality", "started_at"):
            if payload.get(k) is not None:
                out[k] = payload[k]
        return out
