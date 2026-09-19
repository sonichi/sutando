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
        agents = self._entries()
        return {"agents": agents}

    def _entries(self) -> list:
        agents = []
        if self.cores_dir.is_dir():
            for f in sorted(self.cores_dir.glob("*.alive")):
                agents.append(self._entry(f))
        self._annotate_superseded(agents)
        return agents

    @staticmethod
    def _identity_key(entry: dict):
        # Absent start time is not evidence of sameness: pid alone recycles.
        # Only the writer's scalar types qualify — others cannot key a dict.
        pid, started = entry.get("pid"), entry.get("started_at")
        if isinstance(pid, bool) or isinstance(started, bool):
            return None
        if not isinstance(pid, int) or not isinstance(started, (int, float)):
            return None
        return (pid, started)

    def _annotate_superseded(self, agents: list) -> None:
        """A dead heartbeat whose (pid, started_at) matches a LIVE one is the
        same process under a stale host label — annotate, never hide or delete."""
        live = {}
        for a in agents:
            k = self._identity_key(a)
            if k is not None and a.get("alive"):
                live.setdefault(k, a["agentId"])
        for a in agents:
            if a.get("alive"):
                continue
            k = self._identity_key(a)
            holder = live.get(k) if k is not None else None
            if holder is not None and holder != a["agentId"]:
                a["supersededBy"] = holder

    def agent_status(self, agent_id: str) -> dict | None:
        """Status for one agent, or None if no heartbeat file matches.
        `agent_id` matches the heartbeat basename (the per-host label) or the
        payload's self-reported host."""
        if not agent_id:
            return None
        # Built from the full set so a superseded entry reports it here too.
        for entry in self._entries():
            if agent_id in (entry.get("agentId"), entry.get("host")):
                return entry
        return None

    def _entry(self, f: Path) -> dict:
        try:
            age = time.time() - f.stat().st_mtime
        except OSError:  # unlinked between glob and stat — offline
            return {"agentId": f.stem, "alive": False}
        try:
            payload = json.loads(f.read_text())
        except (OSError, ValueError):
            payload = {}
        out = {
            "agentId": f.stem,
            "alive": age < ALIVE_MAX_AGE_S,
            "beatAgeS": round(age, 1),
        }
        for k in ("host", "pid", "status", "socket", "locality", "started_at"):
            if payload.get(k) is not None:
                out[k] = payload[k]
        return out
