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
from uuid import uuid4

REFRESH_S = 30.0

_ASSIGN_RE = re.compile(r"\.(?:assigned|claimed)-(.+)\.txt$")


class PoolStatusWriter:
    def __init__(self, tasks_dir, state_dir, followers_fn, alive_fn,
                 now_fn=time.time, refresh_s: float = REFRESH_S,
                 bindings_fn=None):
        self.tasks_dir = Path(tasks_dir)
        self.state_dir = Path(state_dir)
        self.followers_fn = followers_fn
        self.alive_fn = alive_fn
        self.now = now_fn
        self.refresh_s = refresh_s
        self.bindings_fn = bindings_fn
        self._last_write = 0.0
        self._last_key = None

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
        snap = {
            "ts": int(self.now()),
            "writer": "pool-lead",
            "live_workers": live,
            "dead_workers": dead,
            # The snapshot rides verbatim to the broker; old keys stay one release.
            "live_cores": live,
            "dead_cores": dead,
            "in_flight": self._in_flight(),
        }
        if self.bindings_fn is not None:
            snap["bindings"] = {
                ch: {"instance": row.get("instance"),
                     "pinned": bool(row.get("pinned")),
                     "dedicated": bool(row.get("exclusive"))}
                for ch, row in self.bindings_fn().items()
                if isinstance(row, dict)}
        return snap

    def maybe_write(self) -> bool:
        """Push-on-change with a heartbeat: write immediately when content
        changed, else only after the throttle window (readers can trust ts).
        Fail-open: a status-write error must never break scheduling."""
        snap = self.snapshot()
        key = json.dumps({k: v for k, v in snap.items() if k != "ts"},
                         sort_keys=True)
        if (key == self._last_key
                and self.now() - self._last_write < self.refresh_s):
            return False
        p = self._path()
        # Per-writer temp name: a shared one lets a second lead consume it
        # between write and rename, and the loser's replace fails silently.
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(snap, indent=2))
            os.replace(tmp, p)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            return False
        self._last_write = self.now()
        self._last_key = key
        return True
