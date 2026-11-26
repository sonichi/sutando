"""Whether the owner already sees the core's blocked card: the monitor (core-input-watch.py)
raises it for CARD_STATES, the s.space bridge projects it, and the relay reads this before
it would post a second alert for the same block."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

#: The core states its monitor raises a blocked card for.
CARD_STATES = frozenset({"blocked-human", "logged-out"})
SOURCE = "tui"

SHOWN, PENDING, FAILED, NONE = "shown", "pending", "failed", "none"


def is_limit_episode(kind: Optional[str], prompt: Optional[str]) -> bool:
    """A usage limit is one episode per session, whatever its screen shows."""
    from delivery.pane_gate import is_usage_limit
    return is_usage_limit(kind, prompt)


def episode_guard(session: str, state: str, kind: Optional[str], prompt: Optional[str]) -> str:
    """The episode key of a block: the guard the monitor's card for it carries."""
    from .tui_gate import guard_for, limit_guard
    return limit_guard(session) if is_limit_episode(kind, prompt) else guard_for(session, prompt, state)


def card_status(store_root: Union[str, Path], session: str, state: str,
                kind: Optional[str] = None, prompt: Optional[str] = None) -> str:
    """For the core session `session` blocked in `state` on `prompt`: SHOWN (this block's card
    is on the owner's surface), PENDING (raised, first projection still to land), FAILED
    (projection given up on) or NONE (no card raised for this block).

    Only a card raised for this episode counts, so one left from an earlier block of the same
    session never stands in for a new one. A pool seat's card (subject `seat`) and a
    policy-answered record are not the core's. Any failure reads NONE.
    """
    if state not in CARD_STATES or not session:
        return NONE
    try:
        from .manager import POLICY_DECIDER, HitlManager, HitlStore
        guard = episode_guard(session, state, kind, prompt)
        root = Path(store_root)
        if not root.is_dir():
            return NONE
        manager = HitlManager(HitlStore(root))
        best = NONE
        for r in manager.active():
            subj = r.subject or {}
            if (subj.get("source") != SOURCE or subj.get("session") != session or subj.get("seat")
                    or subj.get("supervisor_state") != state or r.guard != guard
                    or r.decided_by == POLICY_DECIDER):
                continue
            if manager.store.projection(r.id).get("revision", 0) > 0:
                return SHOWN
            if not manager.projection_failed(r.id):
                best = PENDING
            elif best == NONE:
                best = FAILED
        return best
    except Exception:  # noqa: BLE001 - unknown means the relay's own alert still goes out
        return NONE
