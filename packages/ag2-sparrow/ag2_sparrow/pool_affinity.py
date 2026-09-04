#!/usr/bin/env python3
"""Which worker a channel is pinned to — the one reader of `pool/affinity.json`.

Two callers need this answer and must agree: the bridge, deciding at ingress
whether to name a task for a worker, and the follower claim path, deciding
whether to leave a task alone. A second copy of the rule is how the two start
disagreeing about who owns a room.

Dependency-light on purpose (json, re, pathlib, time only): the bridge imports
it as a vendored package module, `src/` imports it flat.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

# A worker whose beat is older than this is not routed to. Matches the
# follower's own staleness view; a pin must never outlive its worker.
ALIVE_WINDOW_S = 90.0

def read_bindings(state_dir) -> dict:
    """The pin table as a snapshot. Unreadable or malformed reads as empty:
    routing must degrade to the unpinned path, never raise into a caller."""
    try:
        raw = (Path(state_dir) / "pool" / "affinity.json").read_text(encoding="utf-8")
        table = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    return table if isinstance(table, dict) else {}


def pinned_instance(bindings: dict, channel_id: Optional[str]) -> Optional[str]:
    """The instance a channel is EXPLICITLY pinned to, or None.

    Only `pinned: true` counts. A bare sticky entry is a decayed handler rather
    than an owner's binding, and must not constrain routing.
    """
    if not bindings or not channel_id:
        return None
    entry = bindings.get(channel_id)
    if isinstance(entry, dict) and entry.get("pinned") is True:
        return entry.get("instance") or None
    return None


def instance_alive(state_dir, instance: str, now_fn=time.time) -> bool:
    """A missing OR future-dated beat is dead: clock skew must degrade to
    'route to nobody in particular', never pin work to a silent worker."""
    if not instance:
        return False
    try:
        age = now_fn() - (Path(state_dir) / "cores" / f"{instance}.alive").stat().st_mtime
    except OSError:
        return False
    return 0 <= age < ALIVE_WINDOW_S


def route_to(state_dir, channel_id: Optional[str], now_fn=time.time) -> Optional[str]:
    """The worker this channel should go to, or None for 'leave it unassigned'.

    None is the safe answer and the common one: unpinned channels, a dead
    pinned worker, an unreadable table. Naming a dead worker would strand the
    task, which is worse than the scatter the pin exists to prevent.
    """
    inst = pinned_instance(read_bindings(state_dir), channel_id)
    return inst if inst and instance_alive(state_dir, inst, now_fn) else None
