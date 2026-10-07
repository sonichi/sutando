"""Follow-up notices for a blocking requirement the owner has not cleared.

The card is one message, and every later revision edits it in place, which
notifies nobody. A runtime that stays blocked therefore re-alerts the owner as
a NEW message on a backoff, and announces its recovery as one, so a block that
outlives the first alert is never silent. This module owns that policy; the
projector sends what it returns and the projection ledger records it.

Ledger keys (beside `revision` / `event_id`): `notified_at` (epoch of the last
owner-facing message), `reminders` (count sent), `recovered` (notice sent).
"""

from __future__ import annotations

from typing import Dict, Optional

from .schema import STATUS_RESOLVED, TERMINAL_STATUSES, HumanRequirement

# Kinds that stop the runtime outright; a decision card waits without re-alerting.
REMIND_KINDS = frozenset({"auth", "core-blocked"})
# Gap before reminder 1, 2, 3...; the last value repeats.
REMIND_INTERVALS_S = (30 * 60, 2 * 60 * 60, 6 * 60 * 60)

REMINDER = "reminder"
RECOVERY = "recovery"


def on_create(req: HumanRequirement, now: float) -> Dict:
    """Ledger fields written with the CREATE projection."""
    if req.status in TERMINAL_STATUSES:
        return {"recovered": True}  # the card itself already says it is over
    return {"notified_at": now, "reminders": 0}


def interval(reminders: int) -> float:
    return REMIND_INTERVALS_S[min(max(reminders, 0), len(REMIND_INTERVALS_S) - 1)]


def due(req: HumanRequirement, projection: Dict, now: float) -> Optional[str]:
    """REMINDER, RECOVERY, or None. Only once the card exists and is current."""
    if req.kind not in REMIND_KINDS or not projection.get("event_id"):
        return None
    if projection.get("revision", 0) < req.revision:
        return None  # the edit goes first
    if req.status == STATUS_RESOLVED:
        # Only a card this module scheduled owes a recovery; a pre-upgrade ledger has no notified_at.
        owed = projection.get("notified_at") and not projection.get("recovered")
        return RECOVERY if owed else None
    if req.status in TERMINAL_STATUSES:
        return None
    # A card projected before this ledger key existed counts from its creation.
    last = projection.get("notified_at") or req.created_at
    if now - last >= interval(int(projection.get("reminders") or 0)):
        return REMINDER
    return None


def after(notice: str, projection: Dict, now: float) -> Dict:
    """Ledger fields to record once `notice` was accepted."""
    if notice == RECOVERY:
        return {"recovered": True}
    return {"notified_at": now, "reminders": int(projection.get("reminders") or 0) + 1}


def _duration(seconds: float) -> str:
    minutes = max(int(seconds // 60), 1)
    hours, minutes = divmod(minutes, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _queued(req: HumanRequirement) -> int:
    return len(req.blocked_task_ids or [])


def _headline(req: HumanRequirement) -> str:
    first = next((ln.strip() for ln in (req.message or "").splitlines() if ln.strip()), "")
    return (req.title or first or req.kind).rstrip(".")


def body(notice: str, req: HumanRequirement, now: float) -> str:
    n = _queued(req)
    if notice == RECOVERY:
        if n:
            return f"✓ Reconnected — resuming {n} queued task{'s' if n != 1 else ''}."
        return "✓ Reconnected — Sutando has continued its work."
    waiting = f" {n} task{'s are' if n != 1 else ' is'} waiting." if n else ""
    return (f"⚠ Still blocked after {_duration(now - req.created_at)} — "
            f"{_headline(req)}.{waiting} The card above has the way to clear it.")


def dedupe_key(notice: str, req: HumanRequirement, projection: Dict) -> str:
    if notice == RECOVERY:
        return f"hitl:{req.id}:recovered"
    return f"hitl:{req.id}:remind:{int(projection.get('reminders') or 0) + 1}"
