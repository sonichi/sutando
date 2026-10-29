"""Projects HumanRequirement state into Matrix via an injected sender.

Requirement state is authority; every Matrix event is a projection of one
revision. First projection of a requirement is a CREATE (op:message carrying
the space.ag2.hitl content field, same additive extra_content mechanism as
the a2ui review card); every later revision is an EDIT targeting the CREATE
event. The Manager's projection ledger makes this idempotent: a send that
fails records nothing and is retried whole on the next drive; a duplicate
drive re-sends with the same dedupe key and the gateway absorbs it.

The sender is adapter-injected (a callable posting one room-op payload and
returning the gateway's dict answer) — this module never imports a bridge.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

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
    wire_safe,
)

Sender = Callable[[Dict], Dict]
Clock = Callable[[], float]

# A card the relay refuses outright is retried this many times, spaced out, then marked failed.
PROJECTION_MAX_REFUSALS = 5
PROJECTION_RETRY_BASE_S = 30.0
PROJECTION_RETRY_MAX_S = 600.0
# The only answers that judge one payload; every other status is the endpoint's or the transport's.
_CARD_4XX = frozenset({400, 413, 422})
# (store root, id, revision) -> monotonic retry deadline; the persisted retry_at serves a restart.
_RETRY_DEADLINES: Dict[Tuple[str, str, int], float] = {}
# Seconds (monotonic) from the first refusal since an accepted send during which a pass that
# accepts nothing charges no card, the relay being as likely at fault; past it, each is charged.
PROJECTION_ENDPOINT_GRACE_S = 600.0
# store root -> monotonic time of the first refusal since the relay last accepted a send.
_NO_ACCEPT_SINCE: Dict[str, float] = {}


class ProjectionPass(NamedTuple):
    """One drive: accepted [(id, event_id)], refused [(id, attempt, error, retry_in_s)] still
    to be retried, and failed [(id, revision, error)] newly given up on."""
    done: List[Tuple[str, Optional[str]]]
    refused: List[Tuple[str, int, str, float]]
    failed: List[Tuple[str, int, str]]

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
# A cleared Claude login is a sign-in; "reconnect" names restoring an AG2 Space agent token.
_KIND_STATUS_LINES = {("auth", STATUS_RESOLVED): "✓ Signed in — Sutando has continued its work."}


def fallback_body(req: HumanRequirement) -> str:
    """Plain-text rendering for clients that ignore the hitl content field."""
    device = req.wire_device() or {}
    name, host = device.get("name", ""), device.get("host", "")
    where = f" (on {host} · {name})" if host and name else f" (on {host or name})" if (host or name) else ""
    lead = _CATEGORY_HEADS.get(category_of(req.kind),
                               _CATEGORY_HEADS[CATEGORY_BLOCKED])
    head = f"{lead} — {req.message}{where}"
    closer = _KIND_STATUS_LINES.get((req.kind, req.status)) or _STATUS_LINES.get(req.status)
    if closer:
        return f"{head}\n\n{closer}"
    return head


def _notices(manager: HitlManager, now: float) -> List[renotify.Notice]:
    entries = [(r, manager.store.projection(r.id)) for r in manager.store.all()
               if r.decided_by is None]
    return renotify.plan(entries, now) + renotify.failures(entries)


def _refusal_code(exc: BaseException) -> Optional[int]:
    """The status with which the relay refused this one payload, or None for anything else."""
    code = getattr(exc, "code", None)
    code = code if isinstance(code, int) else getattr(exc, "status", None)
    return code if code in _CARD_4XX else None


def _detail(describe: Optional[Callable[[BaseException], str]], exc: BaseException) -> str:
    try:
        return " ".join(str(describe(exc) or "").split()) if describe else ""
    except Exception:  # noqa: BLE001 — a body that cannot be read never hides the refusal
        return ""


def _backing_off(manager: HitlManager, req: HumanRequirement, now: float, mono: float) -> bool:
    key = (str(manager.store.root), req.id, req.revision)
    ledger = manager.store.projection(req.id)
    if ledger.get("refused_revision") != req.revision or ledger.get("failed_revision") == req.revision:
        _RETRY_DEADLINES.pop(key, None)
        return False
    if key in _RETRY_DEADLINES:
        return mono < _RETRY_DEADLINES[key]
    retry_at = ledger.get("retry_at") or 0
    # Past the longest wait from now, the deadline was written under a skewed clock: due now.
    return now < retry_at <= now + PROJECTION_RETRY_MAX_S


def _refuse(manager: HitlManager, req: HumanRequirement, code: int, detail: str, now: float,
            mono: float, out: "ProjectionPass") -> None:
    error = f"HTTP {code}: {detail[:200]}" if detail else f"HTTP {code}"
    delays: Dict[int, float] = {}

    def retry_fields(n: int) -> Dict:
        delays[n] = min(PROJECTION_RETRY_BASE_S * 2 ** (n - 1), PROJECTION_RETRY_MAX_S)
        return {"retry_at": now + delays[n]}

    got = manager.refuse_projection(req.id, req.revision, error, PROJECTION_MAX_REFUSALS, retry_fields)
    key = (str(manager.store.root), req.id, req.revision)
    if got is None:
        return  # the revision moved on or another writer's send was accepted
    n, failed = got
    if failed:
        _RETRY_DEADLINES.pop(key, None)
        out.failed.append((req.id, req.revision, error))
        return
    _RETRY_DEADLINES[key] = mono + delays[n]
    out.refused.append((req.id, n, error, delays[n]))


def pending_ids(manager: HitlManager, clock: Optional[Clock] = None,
                monotonic: Optional[Clock] = None) -> List[str]:
    """Requirement ids whose revision is ahead of their projection, or that owe
    the owner a follow-up notice (hitl.renotify).

    A caller owning retry cadence needs to tell "nothing to send" from "every
    send was refused"; `project()` returns an empty list for both.

    This WRITES: every terminal record that owes nothing is moved to the
    archive on the way, so the per-pulse scan stays the size of the live set.
    A failed move is skipped, warned once per id, and counted in the status
    health-check reads (`archive_health`); cleanup never blocks delivery.
    """
    now, mono = (clock or time.time)(), (monotonic or time.monotonic)()
    owed = {r.id for n in _notices(manager, now) for r, _ in n.members}
    out: List[str] = []
    failed: Dict[str, BaseException] = {}
    for r in manager.store.all():
        if (manager.needs_projection(r.id) and not _backing_off(manager, r, now, mono)) or r.id in owed:
            out.append(r.id)
        elif r.terminal:
            try:
                manager.archive_if_settled(r.id, now)
            except OSError as e:
                failed[r.id] = e
    manager.store.report_archive_failures(failed, now)
    return out


def project(manager: HitlManager, send: Sender, room_id: str,
            clock: Optional[Clock] = None) -> List[Tuple[str, Optional[str]]]:
    """`project_pass`, returning only the projections accepted this drive."""
    return project_pass(manager, send, room_id, clock).done


def project_pass(manager: HitlManager, send: Sender, room_id: str, clock: Optional[Clock] = None,
                 describe: Optional[Callable[[BaseException], str]] = None,
                 monotonic: Optional[Clock] = None) -> ProjectionPass:
    """Drive every requirement whose revision is ahead of its projection, then
    send each follow-up notice that is due as a NEW message.

    A rejected send is skipped (nothing recorded) so the next drive retries it.
    A 400, 413 or 422 never stops the other cards in the pass. It is charged to its card
    (backed off on its own and, after PROJECTION_MAX_REFUSALS, marked failed for that
    revision) as `_settle_refusals` decides; when no card was accepted the due notices are
    still sent, then the first refusal is re-raised, carrying this pass as `.projection`.
    Any other exception from the sender propagates at once — the caller owns transport retry
    cadence, this module owns idempotency. `describe` renders a refusal's response body for the log.
    """
    clock = clock or time.time
    now, mono = clock(), (monotonic or time.monotonic)()
    out = ProjectionPass([], [], [])
    held: List[Tuple[HumanRequirement, int, str, BaseException]] = []
    for req in manager.store.all():
        if not manager.needs_projection(req.id) or _backing_off(manager, req, now, mono):
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
            answer = send(wire_safe(payload))
        except Exception as e:  # noqa: BLE001 — classified; anything not this card's re-raises
            code = _refusal_code(e)
            if code is None:
                raise
            held.append((req, code, _detail(describe, e), e))
            continue
        if not isinstance(answer, dict) or not (answer.get("ok") or answer.get("event_id")):
            continue
        event_id = str(answer.get("event_id") or "") or None
        extra = None if target else renotify.on_create(req, clock())
        manager.record_projection(req.id, req.revision, event_id if not target else None, extra)
        _RETRY_DEADLINES.pop((str(manager.store.root), req.id, req.revision), None)
        out.done.append((req.id, event_id))
    if out.done:
        _accepted(manager)
    first = _settle_refusals(manager, held, now, mono, out) if held else None
    if first is None:
        _send_notices(manager, send, room_id, clock(), out.done)
        return out
    try:
        _send_notices(manager, send, room_id, clock(), out.done)
    except Exception:  # noqa: BLE001 — the card refusal is what the caller must back off on
        pass
    raise first


def _accepted(manager: HitlManager) -> None:
    _NO_ACCEPT_SINCE.pop(str(manager.store.root), None)


def _settle_refusals(manager: HitlManager, held: List[Tuple[HumanRequirement, int, str, BaseException]],
                     now: float, mono: float, out: ProjectionPass) -> Optional[BaseException]:
    """Charge each refused card unless the refusal may be the relay's; return the refusal to
    re-raise when the relay accepted nothing this pass, so the caller's backoff engages.

    With a card accepted, every refusal is its card's and is charged at once. Otherwise no card,
    however many were refused, is charged until PROJECTION_ENDPOINT_GRACE_S has passed since the
    first refusal after an accepted send; past it each is charged, so a card's own defect still
    ends in failed.
    """
    if out.done:
        for req, code, detail, _ in held:
            _refuse(manager, req, code, detail, now, mono, out)
        return None
    in_grace = mono - _NO_ACCEPT_SINCE.setdefault(str(manager.store.root), mono) < PROJECTION_ENDPOINT_GRACE_S
    if not in_grace:
        for req, code, detail, _ in held:
            _refuse(manager, req, code, detail, now, mono, out)
    first = held[0][3]
    try:
        first.projection, first.endpoint_wide = out, in_grace  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return first


def _send_notices(manager: HitlManager, send: Sender, room_id: str, now: float,
                  out: List[Tuple[str, Optional[str]]]) -> None:
    for n in _notices(manager, now):
        ledger = dict(next(pj for r, pj in n.members if r.id == n.req.id))
        # No card field (a second card would never get the status edits); the notice field lets
        # a client tie this message to its cards and drop it once they are all cleared.
        device = n.req.wire_device() or {}
        notice = {"notice": n.notice, "ids": [r.id for r, _ in n.members],
                  "runtime": n.req.runtime, "device": device.get("id") or "",
                  "host": device.get("host") or ""}
        answer = send(wire_safe({"room_id": room_id, "op": "message",
                                 "body": renotify.body(n.notice, n.req, now, since=n.since, queued=n.queued),
                                 "dedupe_key": renotify.dedupe_key(n.notice, n.req, ledger, n.reminders),
                                 "extra_content": {NOTICE_FIELD: notice}}))
        if not isinstance(answer, dict) or not (answer.get("ok") or answer.get("event_id")):
            continue
        _accepted(manager)
        # Every card the notice covered is stamped, so none of them is ever sent again.
        for r, pj in n.members:
            manager.record_notice(r.id, renotify.after(n.notice, pj, now, n.reminders))
        out.append((n.req.id, str(answer.get("event_id") or "") or None))
