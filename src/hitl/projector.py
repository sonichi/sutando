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

import logging
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
    TERMINAL_STATUSES,
    WIRE_FIELD,
    category_of,
)

Sender = Callable[[Dict], Dict]
Clock = Callable[[], float]
Log = Callable[[str], None]
ErrorBody = Callable[[BaseException], str]

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
    A failed move is logged and skipped; cleanup never blocks delivery.
    """
    now = clock()
    owed = {r.id for n in _notices(manager, now) for r, _ in n.members}
    out: List[str] = []
    for r in manager.store.all():
        if manager.needs_projection(r.id) or r.id in owed:
            out.append(r.id)
        elif r.terminal:
            try:
                manager.archive_if_settled(r.id, now)
            except OSError as e:
                logging.getLogger("hitl.projector").warning(
                    "hitl: could not archive %s: %s", r.id, e)
    return out


def project(manager: HitlManager, send: Sender, room_id: str,
            clock: Clock = time.time, log: Optional[Log] = None,
            error_body: Optional[ErrorBody] = None) -> List[Tuple[str, Optional[str]]]:
    """Drive every requirement whose revision is ahead of its projection, then
    send each follow-up notice that is due as a NEW message.

    Returns [(req_id, event_id_or_None), ...] for the projections that were
    accepted this drive. A rejected or raising send is skipped (nothing
    recorded) so the next drive retries it, and the pass moves on to the next
    card. The exception propagates only when no send in the pass was accepted,
    so the caller, which owns retry cadence, still backs off a refusing relay.
    A terminal card whose status edit is refused with a 4xx is recorded as
    projected: the refusal is final, and retrying it would recur every drive.
    """
    pass_ = _Pass(log, error_body)
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
        try:
            answer = pass_.send(send, payload, req.id)
        except Exception as e:  # noqa: BLE001 — one card must not abort the pass
            if target and req.status in TERMINAL_STATUSES and _client_error(e):
                manager.record_projection(req.id, req.revision, None,
                                          {"edit_refused": getattr(e, "code", None)})
            continue
        if not isinstance(answer, dict) or not (answer.get("ok") or answer.get("event_id")):
            continue
        event_id = str(answer.get("event_id") or "") or None
        extra = None if target else renotify.on_create(req, clock())
        manager.record_projection(req.id, req.revision, event_id if not target else None, extra)
        out.append((req.id, event_id))
    out.extend(_send_notices(manager, send, room_id, clock(), pass_))
    pass_.raise_if_all_failed()
    return out


# Genuinely final only; 401/403/408/429 are blips, not refusals that would
# recur -- settling on them would archive a card whose edit never landed.
_TERMINAL_CODES = frozenset({400, 404, 409, 410, 422})


def _client_error(e: BaseException) -> bool:
    return getattr(e, "code", None) in _TERMINAL_CODES


class _Pass:
    """Per-drive send accounting: each send is isolated, and the drive raises
    the last error only if every attempted send raised or was refused."""

    def __init__(self, log: Optional[Log], error_body: Optional[ErrorBody]):
        self._log = log or logging.getLogger("hitl.projector").warning
        self._error_body = error_body
        self.accepted = False
        self.error: Optional[BaseException] = None

    def send(self, send: Sender, payload: Dict, req_id: str) -> Dict:
        try:
            answer = send(payload)
        except Exception as e:  # noqa: BLE001
            self.error = e
            body = (self._error_body(e) if self._error_body else "")[:200]
            self._log(f"hitl: send for {req_id} failed: {e}" + (f" — {body}" if body else ""))
            raise
        if isinstance(answer, dict) and (answer.get("ok") or answer.get("event_id")):
            self.accepted = True
        return answer

    def raise_if_all_failed(self) -> None:
        if self.error is not None and not self.accepted:
            raise self.error

    def log(self, message: str) -> None:
        self._log(message)


def _send_notices(manager: HitlManager, send: Sender, room_id: str,
                  now: float, pass_: _Pass) -> List[Tuple[str, Optional[str]]]:
    out: List[Tuple[str, Optional[str]]] = []
    for n in _notices(manager, now):
        ledger = dict(next(pj for r, pj in n.members if r.id == n.req.id))
        # No card field (a second card would never get the status edits); the notice field lets
        # a client tie this message to its cards and drop it once they are all cleared.
        device = n.req.wire_device() or {}
        notice = {"notice": n.notice, "ids": [r.id for r, _ in n.members],
                  "runtime": n.req.runtime, "device": device.get("id") or "",
                  "host": device.get("host") or ""}
        try:
            body = renotify.body(n.notice, n.req, now, since=n.since, queued=n.queued)
            dedupe_key = renotify.dedupe_key(n.notice, n.req, ledger, n.reminders)
        except Exception as e:  # noqa: BLE001 — logged, not swallowed; the next notice still goes
            pass_.log(f"hitl: notice build for {n.req.id} failed: {e}")
            continue
        try:
            answer = pass_.send(send, {
                "room_id": room_id, "op": "message",
                "body": body, "dedupe_key": dedupe_key,
                "extra_content": {NOTICE_FIELD: notice}}, n.req.id)
        except Exception:  # noqa: BLE001 — logged by the pass; the next notice still goes
            continue
        if not isinstance(answer, dict) or not (answer.get("ok") or answer.get("event_id")):
            continue
        # Every card the notice covered is stamped, so none of them is ever sent again.
        for r, pj in n.members:
            manager.record_notice(r.id, renotify.after(n.notice, pj, now, n.reminders))
        out.append((n.req.id, str(answer.get("event_id") or "") or None))
    return out
