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

import time
from typing import Callable, Dict, List, Optional, Tuple

from . import renotify
from .manager import HitlManager
from .schema import (
    CATEGORY_BLOCKED,
    CATEGORY_DECISION,
    HumanRequirement,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_RESOLVED,
    NOTICE_FIELD,
    WIRE_FIELD,
    category_of,
)

Sender = Callable[[Dict], Dict]
Clock = Callable[[], float]

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
    device = req.wire_device() or {}
    name, host = device.get("name", ""), device.get("host", "")
    where = f" (on {host} · {name})" if host and name else f" (on {host or name})" if (host or name) else ""
    lead = _CATEGORY_HEADS.get(category_of(req.kind),
                               _CATEGORY_HEADS[CATEGORY_BLOCKED])
    head = f"{lead} — {req.message}{where}"
    closer = _STATUS_LINES.get(req.status)
    if closer:
        return f"{head}\n\n{closer}"
    return head


def _notices(manager: HitlManager, now: float) -> List[renotify.Notice]:
    entries = [(r, manager.store.projection(r.id)) for r in manager.store.all()
               if r.decided_by is None]
    return renotify.plan(entries, now)


def pending_ids(manager: HitlManager, clock: Clock = time.time) -> List[str]:
    """Requirement ids whose revision is ahead of their projection, or that owe
    the owner a follow-up notice (hitl.renotify).

    A caller owning retry cadence needs to tell "nothing to send" from "every
    send was refused"; `project()` returns an empty list for both.

    This WRITES: every terminal record that owes nothing is moved to the
    archive on the way, so the per-pulse scan stays the size of the live set.
    A failed move is skipped, warned once per id, and counted in the status
    health-check reads (`archive_health`); cleanup never blocks delivery.
    """
    now = clock()
    owed = {r.id for n in _notices(manager, now) for r, _ in n.members}
    out: List[str] = []
    failed: Dict[str, BaseException] = {}
    for r in manager.store.all():
        if manager.needs_projection(r.id) or r.id in owed:
            out.append(r.id)
        elif r.terminal:
            try:
                manager.archive_if_settled(r.id, now)
            except OSError as e:
                failed[r.id] = e
    manager.store.report_archive_failures(failed, now)
    return out


def project(manager: HitlManager, send: Sender, room_id: str,
            clock: Clock = time.time) -> List[Tuple[str, Optional[str]]]:
    """Drive every requirement whose revision is ahead of its projection, then
    send each follow-up notice that is due as a NEW message.

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
        extra = None if target else renotify.on_create(req, clock())
        manager.record_projection(req.id, req.revision, event_id if not target else None, extra)
        out.append((req.id, event_id))
    out.extend(_send_notices(manager, send, room_id, clock()))
    return out


def _send_notices(manager: HitlManager, send: Sender, room_id: str,
                  now: float) -> List[Tuple[str, Optional[str]]]:
    out: List[Tuple[str, Optional[str]]] = []
    for n in _notices(manager, now):
        ledger = dict(next(pj for r, pj in n.members if r.id == n.req.id))
        # No card field (a second card would never get the status edits); the notice field lets
        # a client tie this message to its cards and drop it once they are all cleared.
        device = n.req.wire_device() or {}
        notice = {"notice": n.notice, "ids": [r.id for r, _ in n.members],
                  "runtime": n.req.runtime, "device": device.get("id") or "",
                  "host": device.get("host") or ""}
        answer = send({"room_id": room_id, "op": "message",
                       "body": renotify.body(n.notice, n.req, now, since=n.since, queued=n.queued),
                       "dedupe_key": renotify.dedupe_key(n.notice, n.req, ledger, n.reminders),
                       "extra_content": {NOTICE_FIELD: notice}})
        if not isinstance(answer, dict) or not (answer.get("ok") or answer.get("event_id")):
            continue
        # Every card the notice covered is stamped, so none of them is ever sent again.
        for r, pj in n.members:
            manager.record_notice(r.id, renotify.after(n.notice, pj, now, n.reminders))
        out.append((n.req.id, str(answer.get("event_id") or "") or None))
    return out
