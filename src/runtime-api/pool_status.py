#!/usr/bin/env python3
"""Owner-facing pool snapshot (single writer: the lead daemon).

`state/pool-status.json` previously went stale the moment its one-shot
writer stopped running; the lead now refreshes it on a throttle so readers
can trust `ts`. Everything is injected (dirs, follower enumeration,
liveness, clock) for the same testability contract as PoolLead.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

REFRESH_S = 30.0

_ASSIGN_RE = re.compile(r"\.(?:assigned|claimed)-(.+)\.txt$")


class PoolStatusWriter:
    def __init__(self, tasks_dir, state_dir, followers_fn, alive_fn,
                 now_fn=time.time, refresh_s: float = REFRESH_S):
        self.tasks_dir = Path(tasks_dir)
        self.state_dir = Path(state_dir)
        self.followers_fn = followers_fn
        self.alive_fn = alive_fn
        self.now = now_fn
        self.refresh_s = refresh_s
        self._last_write = 0.0

    def _path(self) -> Path:
        return self.state_dir / "pool-status.json"

    def _in_flight(self) -> "dict[str, int]":
        counts: dict = {}
        try:
            entries = list(self.tasks_dir.iterdir())
        except OSError:
            return counts
        for f in entries:
            m = _ASSIGN_RE.search(f.name)
            if m:
                counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        return counts

    def snapshot(self) -> dict:
        followers = list(self.followers_fn())
        live = sorted(f for f in followers if self.alive_fn(f))
        dead = sorted(f for f in followers if f not in live)
        return {
            "ts": int(self.now()),
            "writer": "pool-lead",
            "live_cores": live,
            "dead_cores": dead,
            "in_flight": self._in_flight(),
        }

    def maybe_write(self) -> bool:
        """Refresh the snapshot if the throttle window has passed.
        Fail-open: a status-write error must never break scheduling."""
        if self.now() - self._last_write < self.refresh_s:
            return False
        try:
            p = self._path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.snapshot(), indent=2))
            os.replace(tmp, p)
        except OSError:
            return False
        self._last_write = self.now()
        return True
