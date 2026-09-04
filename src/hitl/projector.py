"""Projects HumanRequirement state into Matrix via an injected sender.

Requirement state is authority; every Matrix event is a projection of one
revision. First projection of a requirement is a CREATE (op:message carrying
the space.ag2.hitl content field, same additive extra_content mechanism as
the a2ui review card); every later revision is an EDIT targeting the CREATE
event. The Manager's projection ledger makes this idempotent: a send that
fails records nothing and is retried whole on the next drive; a duplicate
drive re-sends with the same dedupe key and the gateway absorbs it.

The sender is adapter-injected (a callable posting one /v1/room payload and
returning the gateway's dict answer) — this module never imports a bridge.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from .manager import HitlManager
from .schema import (
    CATEGORY_BLOCKED,
    CATEGORY_DECISION,
    HumanRequirement,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_RESOLVED,
    WIRE_FIELD,
    category_of,
)

Sender = Callable[[Dict], Dict]

# The card colours these two apart; a client that ignores the card field saw the
# blocking header for every kind, so a mere decision read as an alarm.
_CATEGORY_HEADS = {
    CATEGORY_BLOCKED: "⚠ Sutando needs your attention",
    CATEGORY_DECISION: "Sutando needs a decision",
}

_STATUS_LINES = {
    STATUS_RESOLVED: "✓ Resolved — Sutando has continued its work.",
    STATUS_EXPIRED: "— Expired, no longer applicable.",
    STATUS_CANCELLED: "— Cancelled.",
}


def fallback_body(req: HumanRequirement) -> str:
    """Plain-text rendering for clients that ignore the hitl content field."""
    device = (req.device or {}).get("name", "")
    where = f" (on {device})" if device else ""
    head = f"{_CATEGORY_HEADS[category_of(req.kind)]} — {req.message}{where}"
    closer = _STATUS_LINES.get(req.status)
    if closer:
        return f"{head}\n\n{closer}"
    return head


def pending_ids(manager: HitlManager) -> List[str]:
    """Requirement ids whose revision is ahead of their projection.

    A caller owning retry cadence needs to tell "nothing to send" from "every
    send was refused"; `project()` returns an empty list for both.
    """
    return [r.id for r in manager.store.all() if manager.needs_projection(r.id)]


def project(manager: HitlManager, send: Sender, room_id: str) -> List[Tuple[str, Optional[str]]]:
    """Drive every requirement whose revision is ahead of its projection.

    Returns [(req_id, event_id_or_None), ...] for the projections that were
    accepted this drive. A rejected send is skipped (nothing recorded) so the
    next drive retries it; an exception from the sender propagates — the
    caller owns retry cadence, this module owns idempotency.
    """
    out: List[Tuple[str, Optional[str]]] = []
    for req in manager.store.all():
        if not manager.needs_projection(req.id):
            continue
        target = manager.projection_target(req.id)
        payload: Dict = {
            "room_id": room_id,
            "body": fallback_body(req),
            "dedupe_key": f"hitl:{req.id}:{req.revision}",
            "extra_content": {WIRE_FIELD: req.to_wire()},
        }
        if target:
            payload.update({"op": "edit", "event_id": target})
        else:
            payload["op"] = "message"
        answer = send(payload)
        if not isinstance(answer, dict) or not (answer.get("ok") or answer.get("event_id")):
            continue
        event_id = str(answer.get("event_id") or "") or None
        manager.record_projection(req.id, req.revision, event_id if not target else None)
        out.append((req.id, event_id))
    return out
