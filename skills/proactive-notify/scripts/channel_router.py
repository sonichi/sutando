"""Pick a delivery channel for a Ping given the presence snapshot + policy.

Walk overrides in order; first whose `if:` predicate evaluates True on the
snapshot wins. Return its behavior mapping. Falls through to default_channel.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from presence import PresenceSnapshot


@dataclass
class Ping:
    name: str
    urgency: str  # "critical" | "important" | "fyi"
    voice_natural: bool
    body: str
    dedup_key: str
    prefer_channel: Optional[str] = None
    quiet_hours_override: bool = False


def route(ping: Ping, presence: PresenceSnapshot, policy: dict) -> str:
    """Return the action-channel name (e.g. 'sms', 'call', 'queue')."""
    if ping.prefer_channel:
        return ping.prefer_channel

    for override in policy.get("overrides") or []:
        predicate = override.get("if")
        if not predicate:
            continue
        if not getattr(presence, predicate, False):
            continue
        # quiet-hours override may be bypassed by critical ping with override.
        if override.get("name") == "in_quiet_hours_downgrades" and ping.quiet_hours_override and ping.urgency == "critical":
            continue
        behavior = override.get("behavior") or {}
        chosen = behavior.get(ping.urgency)
        if chosen:
            return chosen

    defaults = policy.get("default_channel") or {}
    return defaults.get(ping.urgency, "queue")
