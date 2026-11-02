"""Follow-up notices for a blocking requirement the owner has not cleared.

The card is one message, and every later revision edits it in place, which
notifies nobody. A runtime that stays blocked therefore re-alerts the owner as
a NEW message on a backoff, and announces its recovery as one, so a block that
outlives the first alert is never silent. This module owns that policy; the
projector sends what it returns and the projection ledger records it.

Notices are per seat (runtime + device), not per card: a seat that stays blocked
can mint a card per repaint, and the owner must hear once that the seat is still
blocked or has recovered, not once per card.

A card the relay refused past the retry bound is announced once per revision as
a plain message, since the owner never saw the card itself.

Ledger keys (beside `revision` / `event_id`): `notified_at` (epoch of the last
owner-facing message), `reminders` (count sent), `recovered` (notice sent),
`failed_notice` (the failed revision announced).
"""

from __future__ import annotations

from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

from .schema import STATUS_RESOLVED, TERMINAL_STATUSES, HumanRequirement

# Kinds that stop the runtime outright; a decision card waits without re-alerting.
REMIND_KINDS = frozenset({"auth", "core-blocked"})
# Gap before reminder 1, 2, 3...; the last value repeats.
REMIND_INTERVALS_S = (30 * 60, 2 * 60 * 60, 6 * 60 * 60)

REMINDER = "reminder"
RECOVERY = "recovery"
FAILED = "failed"
_FAILED_HEADLINE_MAX = 200
# Kinds whose recovery is a sign-in, announced without the card's headline; others say what cleared.
SIGN_IN_KINDS = frozenset({"auth"})


class Notice(NamedTuple):
    """One owner-facing message covering every card of one seat. `req` is the newest
    card, whose headline speaks for the seat; `since` is when the seat's block began;
    `queued` counts distinct waiting tasks; `reminders` is the seat's count after this one."""
    notice: str
    req: HumanRequirement
    members: List[Tuple[HumanRequirement, Dict]]
    since: float
    queued: int
    reminders: int


def on_create(req: HumanRequirement, now: float) -> Dict:
    """Ledger fields written with the CREATE projection."""
    if req.status in TERMINAL_STATUSES:
        return {"recovered": True}  # the card itself already says it is over
    return {"notified_at": now, "reminders": 0}


def reminds(req: HumanRequirement) -> bool:
    """A card whose subject says `remind: False` is announced once: its card is the only alert."""
    return (req.subject or {}).get("remind") is not False


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
    if req.status in TERMINAL_STATUSES or not reminds(req):
        return None
    # A card projected before this ledger key existed counts from its creation.
    last = projection.get("notified_at") or req.created_at
    if now - last >= interval(int(projection.get("reminders") or 0)):
        return REMINDER
    return None


def after(notice: str, projection: Dict, now: float, reminders: Optional[int] = None) -> Dict:
    """Ledger fields to record once `notice` was accepted."""
    if notice == FAILED:
        return {"failed_notice": projection.get("failed_revision")}
    if notice == RECOVERY:
        return {"recovered": True}
    if reminders is None:
        reminders = int(projection.get("reminders") or 0) + 1
    return {"notified_at": now, "reminders": reminders}


def seat_key(req: HumanRequirement) -> Tuple[str, str]:
    return (req.runtime, str((req.device or {}).get("id") or ""))


def _current(req: HumanRequirement, projection: Dict) -> bool:
    return bool(projection.get("event_id")) and projection.get("revision", 0) >= req.revision


def _pending_resolve(req: HumanRequirement, projection: Dict) -> bool:
    """A resolved, notified, not-yet-recovered card whose own edit hasn't landed.

    Its seat's recovery must wait for it, not fire around it: a rejected send
    retries on the next drive, and skipping ahead here is what splits one
    seat's recovery into two messages."""
    if req.status != STATUS_RESOLVED or not projection.get("event_id"):
        return False
    if projection.get("revision", 0) >= req.revision:
        return False
    return bool(projection.get("notified_at")) and not projection.get("recovered")


def _tasks(members: Iterable[Tuple[HumanRequirement, Dict]]) -> int:
    return len({t for r, _ in members for t in (r.blocked_task_ids or [])})


def plan(entries: Iterable[Tuple[HumanRequirement, Dict]], now: float) -> List[Notice]:
    """The notices owed now, at most one per seat, from (requirement, ledger) pairs.

    A seat with any card still open owes no recovery yet; a seat's reminder runs on
    one clock, the latest alert and highest count across its open cards."""
    seats: Dict[Tuple[str, str], List[Tuple[HumanRequirement, Dict]]] = {}
    for req, pj in entries:
        if req.kind in REMIND_KINDS:
            seats.setdefault(seat_key(req), []).append((req, pj))
    out: List[Notice] = []
    for members in seats.values():
        open_ = [(r, pj) for r, pj in members if r.status not in TERMINAL_STATUSES]
        if open_:
            live = [(r, pj) for r, pj in open_ if _current(r, pj) and reminds(r)]
            if not live:
                continue
            last = max(pj.get("notified_at") or r.created_at for r, pj in live)
            count = max(int(pj.get("reminders") or 0) for _, pj in live)
            if now - last < interval(count):
                continue
            newest = max(live, key=lambda m: m[0].created_at)[0]
            out.append(Notice(REMINDER, newest, live, min(r.created_at for r, _ in open_),
                              _tasks(open_), count + 1))
            continue
        owed = [(r, pj) for r, pj in members if due(r, pj, now) == RECOVERY]
        if owed and not any(_pending_resolve(r, pj) for r, pj in members):
            newest = max(owed, key=lambda m: m[0].created_at)[0]
            out.append(Notice(RECOVERY, newest, owed, min(r.created_at for r, _ in owed),
                              _tasks(owed), 0))
    return out


def failures(entries: Iterable[Tuple[HumanRequirement, Dict]]) -> List[Notice]:
    """One FAILED notice per card whose current revision was given up on and not yet announced."""
    out: List[Notice] = []
    for req, pj in entries:
        failed = pj.get("failed_revision")
        if failed == req.revision and pj.get("revision", 0) < failed and pj.get("failed_notice") != failed:
            out.append(Notice(FAILED, req, [(req, pj)], req.created_at, _queued(req), 0))
    return out


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


def body(notice: str, req: HumanRequirement, now: float,
         since: Optional[float] = None, queued: Optional[int] = None) -> str:
    n = _queued(req) if queued is None else queued
    if notice == FAILED:
        head = _headline(req)
        head = head if len(head) <= _FAILED_HEADLINE_MAX else head[:_FAILED_HEADLINE_MAX - 1] + "…"
        return f"⚠ Sutando could not show you a card — {head}. The relay refused it."
    if notice == RECOVERY:
        tail = (f"resuming {n} queued task{'s' if n != 1 else ''}" if n
                else "Sutando has continued its work")
        if req.kind in SIGN_IN_KINDS:
            seat = str((req.device or {}).get("name") or "")
            return f"✓ Signed in{f' ({seat})' if seat else ''} — {tail}."
        return f"✓ Cleared — {_headline(req)}; {tail}."
    waiting = f" {n} task{'s are' if n != 1 else ' is'} waiting." if n else ""
    started = req.created_at if since is None else since
    return (f"⚠ Still blocked after {_duration(now - started)} — "
            f"{_headline(req)}.{waiting} The card above has the way to clear it.")


def dedupe_key(notice: str, req: HumanRequirement, projection: Dict,
               reminders: Optional[int] = None) -> str:
    if notice == FAILED:
        return f"hitl:{req.id}:failed:{req.revision}"
    if notice == RECOVERY:
        return f"hitl:{req.id}:recovered"
    if reminders is None:
        reminders = int(projection.get("reminders") or 0) + 1
    return f"hitl:{req.id}:remind:{reminders}"
