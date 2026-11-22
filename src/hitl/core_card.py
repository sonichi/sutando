"""Whether the owner already sees the core's blocked card: the monitor (core-input-watch.py)
raises it for CARD_STATES, the s.space bridge projects it, and the relay reads this before
it would post a second alert for the same block."""
from __future__ import annotations

from pathlib import Path
from typing import Union

#: The core states its monitor raises a blocked card for.
CARD_STATES = frozenset({"blocked-human", "logged-out"})
SOURCE = "tui"

SHOWN, PENDING, FAILED, NONE = "shown", "pending", "failed", "none"


def card_status(store_root: Union[str, Path], session: str, state: str) -> str:
    """For the core session `session` blocked in `state`: SHOWN (its card is on the owner's
    surface), PENDING (raised, first projection still to land), FAILED (projection given up
    on) or NONE (no card raised).

    A pool seat's card (subject `seat`) and a policy-answered record are not the core's.
    Any failure reads NONE.
    """
    if state not in CARD_STATES or not session:
        return NONE
    try:
        from .manager import POLICY_DECIDER, HitlManager, HitlStore
        root = Path(store_root)
        if not root.is_dir():
            return NONE
        manager = HitlManager(HitlStore(root))
        best = NONE
        for r in manager.active():
            subj = r.subject or {}
            if (subj.get("source") != SOURCE or subj.get("session") != session or subj.get("seat")
                    or subj.get("supervisor_state") != state or r.decided_by == POLICY_DECIDER):
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
