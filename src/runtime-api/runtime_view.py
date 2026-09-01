#!/usr/bin/env python3
"""Runtime surface for THIS agent: runtime.health / runtime.details.

Owner taxonomy (notes/sutando-server/protocol-taxonomy-owner-2026-08-08.md):
normal clients see a COARSE state — online / offline / degraded — plus the
current activity; PID, sockets, tmux session and heartbeat internals are
runtime implementation details that belong on a separate diagnostic surface,
not in identity or the protocol center.

  health    {state, currentActivity?, beatAgeS?} — end-user coarse readout
  details   diagnostics: heartbeat payload passthrough (pid, socket, locality,
            started_at, ...), beat age, the daemon's own runtime socket

Sources are the same workspace records the identity surface reads; both views
share the heartbeat-reading recipe via identical dependency injection.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from state_records import read_beat, read_record

from agents_view import ALIVE_MAX_AGE_S

# A beat older than this but younger than ALIVE_MAX_AGE_S means the writer is
# lagging its 30s cadence — alive, but not healthy: degraded.
DEGRADED_BEAT_AGE_S = 60


class RuntimeView:
    def __init__(self, state_dir: str | Path, host_label: str | None = None,
                 runtime_socket: str | None = None):
        self.state_dir = Path(state_dir)
        self.host_label = host_label
        self.runtime_socket = runtime_socket

    # ── runtime.health ──────────────────────────────────────────────────────
    def health(self) -> dict:
        beat = self._own_beat()
        age = beat.get("beatAgeS")
        if age is None:
            return {"state": "offline"}
        if age >= ALIVE_MAX_AGE_S or age < 0:
            # negative = future-dated mtime (clock skew / tampering): a dead
            # core must not render attachable on a clock artifact
            return {"state": "offline", "beatAgeS": age}
        out = {"state": "degraded" if age >= DEGRADED_BEAT_AGE_S else "online",
               "beatAgeS": age}
        step = self._core_status().get("step")
        if step:
            out["currentActivity"] = step
        return out

    # ── runtime.details ─────────────────────────────────────────────────────
    def details(self) -> dict:
        out: dict = dict(self._own_beat())
        if self.runtime_socket:
            out["runtimeSocket"] = self.runtime_socket
        return out

    # ── internals ───────────────────────────────────────────────────────────
    def _core_status(self) -> dict:
        return read_record(self.state_dir / "core-status.json") or {}

    def _own_beat(self) -> dict:
        if not self.host_label:
            return {}
        return read_beat(self.state_dir / "cores" / f"{self.host_label}.alive")
