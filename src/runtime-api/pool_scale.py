#!/usr/bin/env python3
"""Lead-side follower autoscaling policy (lead-follower pool, slice L6).

Owner rule (2026-08-23): when every live worker is saturated and unassigned
work is still queuing, add a follower. Scaling down is the risky direction —
a booted-out worker may hold claims — so it is conservative: only past a quiet
window, and the caller must verify the victim worker holds no claimed tasks.

Pure decision function + a cooldown ledger; executing a decision (running
the idempotent installer) belongs to the daemon. Each follower is a real
Claude session burning usage credits, so `max_n` is a hard cap and the
default stays small. Stdlib only; everything injected.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

BUSY_THRESHOLD = 3   # per-worker in-flight depth that reads as saturated
UP_COOLDOWN_S = 300  # flap guard; DOWN_IDLE_S = full-pool quiet before shrink
DOWN_IDLE_S = 1800


def decide(pending_unassigned: int, in_flight: "dict[str, int]",
           current_n: int, min_n: int, max_n: int,
           last_change_ts: float, last_busy_ts: float, now: float,
           busy_threshold: int = BUSY_THRESHOLD,
           up_cooldown_s: int = UP_COOLDOWN_S,
           down_idle_s: int = DOWN_IDLE_S) -> "int | None":
    """Return the new follower count, or None to hold.

    Scale up: backlog exists, every live worker is at/over threshold, cap and
    cooldown allow. Scale down: the pool has been fully idle for the whole
    quiet window. The caller still owns the no-claims safety check.
    """
    if current_n < 1 or min_n < 1 or max_n < min_n:
        return None
    live = list(in_flight.values())
    if (pending_unassigned > 0 and live
            and all(v >= busy_threshold for v in live)
            and current_n < max_n
            and now - last_change_ts >= up_cooldown_s):
        return current_n + 1
    # `and live` matches the up-branch: all([]) is True, so an empty in_flight
    # (every follower gone) would otherwise read as "idle" and shrink the pool.
    if (current_n > min_n and pending_unassigned == 0 and live
            and all(v == 0 for v in live)
            and now - last_busy_ts >= down_idle_s
            and now - last_change_ts >= down_idle_s):
        return current_n - 1
    return None


_UNASSIGNED_RE = None  # built lazily so the regex stays beside its use


def observe(tasks_dir, followers: "list[str]") -> "tuple[int, dict[str, int]]":
    """(pending_unassigned, in_flight per follower) from one dir scan —
    the same quantities the lead's picker uses, read the same way."""
    global _UNASSIGNED_RE
    import re
    if _UNASSIGNED_RE is None:
        _UNASSIGNED_RE = re.compile(
            r"^task-(?!.*\.(?:assigned|claimed)-)[A-Za-z0-9._~-]+\.txt$")
    pending = 0
    in_flight = {f: 0 for f in followers}
    try:
        entries = list(Path(tasks_dir).iterdir())
    except OSError:
        return 0, in_flight
    for f in entries:
        if _UNASSIGNED_RE.match(f.name):
            pending += 1
            continue
        for inst in followers:
            if (f.name.endswith(f".assigned-{inst}.txt")
                    or f.name.endswith(f".claimed-{inst}.txt")):
                in_flight[inst] += 1
                break
    return pending, in_flight


class ScaleLedger:
    """Remembers when the pool last changed size / was last busy, so the
    cooldowns survive daemon restarts."""

    def __init__(self, state_dir, now_fn=time.time):
        self.path = Path(state_dir) / "pool" / "scale-ledger.json"
        self.now = now_fn

    def load(self) -> "dict[str, float]":
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict):
                return {"last_change_ts": float(d.get("last_change_ts", 0)),
                        "last_busy_ts": float(d.get("last_busy_ts", 0))}
        except (OSError, ValueError, TypeError):
            pass
        return {"last_change_ts": 0.0, "last_busy_ts": 0.0}

    def record(self, *, changed: bool = False, busy: bool = False) -> None:
        d = self.load()
        if changed:
            d["last_change_ts"] = self.now()
        if busy:
            d["last_busy_ts"] = self.now()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, self.path)
        except OSError:
            pass  # fail-open: autoscale is advisory, never blocks the sweep
