"""One bridge per agent identity on a host.

The workspace lock stops two bridges that share a folder. Two installs of one agent (another app's
environment, an older agent folder, a hand-started copy) share no folder, so both polled the relay as the
same agent: each task went to whichever asked first, and the other never saw it.

This lock is keyed by the agent and its gateway (`gateway_instance.binding_digest`, so nothing of either is
readable from its name) and kept beside the per-install key, under the user's config dir, where every
install on the host looks. A bridge that finds a live holder does not poll.

Fail-open: with no identity yet, or no per-install key, there is no lock and `take` answers UNKNOWN.
"""
from __future__ import annotations

from pathlib import Path

from . import gateway_instance, workspace_lock

HELD, TAKEN, UNKNOWN = "held", "taken", "unknown"
ROLE_PREFIX = "bridge-identity."


def place() -> Path:
    """The folder whose locks every install of this user shares (workspace_lock keeps them in state/locks)."""
    return gateway_instance.binding_key_path().parent


def role(identity: str, gateway_url: str) -> str:
    """The lock's name for this agent on this gateway; '' when either is unknown or there is no key."""
    digest = gateway_instance.binding_digest(identity, gateway_url)
    return ROLE_PREFIX + digest[:32] if digest else ""


def take(identity: str, gateway_url: str, declares: "dict | None" = None,
         stale_seconds: int = workspace_lock.DEFAULT_STALE_SECONDS) -> "tuple[str, str, dict]":
    """(HELD | TAKEN | UNKNOWN, the lock's name, the live holder when TAKEN)."""
    try:
        name = role(identity, gateway_url)
        if not name:
            return UNKNOWN, "", {}
        got = workspace_lock.acquire(name, place(), stale_seconds=stale_seconds, declares=declares)
    except OSError:
        return UNKNOWN, "", {}
    if got.status == "deferred":
        return TAKEN, name, dict(got.holder or {})
    return HELD, name, {}


def beat(name: str) -> bool:
    """Refresh the lock; False only when another bridge has taken it (this one was deemed stale)."""
    return bool(workspace_lock.heartbeat(name, place())) if name else True


def release(name: str) -> None:
    if name:
        workspace_lock.release(name, place())


def taken_words(holder: dict) -> str:
    """What the refused bridge says: who holds the identity, in its own words."""
    return ("another bridge on this computer already receives this agent's tasks (pid %s, from %s); this one "
            "will not poll. Stop the other one, or remove this install."
            % (holder.get("pid") or "unknown", holder.get("serving") or "an unknown folder"))
