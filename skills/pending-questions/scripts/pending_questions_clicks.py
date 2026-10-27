"""The owner's decision-card clicks on pending questions, handed to one consumer at a time.

A card for a question is a HITL `choice` requirement with runtime `pending-questions`, guard and
`subject.ask_id` set to the ask id, and `subject.choices` = {action id: consequence}. The bridge
records a click (`chosen_action`) and forwards it as a `hitl_click` task; `clicks` turns each
recorded click into an actionable choice:

- Every pass runs under one exclusive lock beside the HITL store, so the lock's holder is the only
  live claimant. A claim still marked `claiming` was left by a pass that died; it is retried with
  the same token.
- The pass claims the click (a token, saved atomically), then the adapter closes the row as
  Answered in one guarded write that requires the owner's Status to be open and leaves the token
  beside the Closed mark. A refusal whose token is this claim's is its own earlier close;
  any other refusal makes the click stale (card cancelled). A close the room cannot verify (an
  outage) defers the click: nothing is handed over and the claim stays to be retried.
- On a close the claim is recorded `handed` with a new lease BEFORE the choice is returned. The
  lease is pending_questions_lease's elapsed-time lease: it expires once HANDED_LEASE_SEC has been
  credited across passes, each pass crediting at most LEASE_MAX_STEP_SEC and nothing for a
  backward step, so no wall-clock step can expire or stretch it. Later passes report the claim as
  "awaiting ack"; `ack_click` with its token settles it. Once expired it is re-offered ONCE with a
  fresh token and a new lease: a choice is delivered at most twice, so a consumer re-checks current
  state before acting. After that single re-offer an expired claim is listed as needing attention,
  never offered again. An ack carrying a superseded token is refused.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import pending_questions_lease as lease_mod

CARD_RUNTIME = "pending-questions"
CLAIMING, HANDED = "claiming", "handed"
CLOSED, STALE, DEFERRED = "closed", "stale", "deferred"
LOCK_NAME = "pending-questions-clicks.lock"
# A handed choice not acked within this long is offered again, once: a consumer that died after
# the handoff must not strand it, and no choice is delivered more than twice.
HANDED_LEASE_SEC = 600
# The most one pass credits: the health check's launchd fallback runs every 300 s, so on a host
# where it runs real and credited time agree, and no single clock jump spends the whole lease.
LEASE_MAX_STEP_SEC = 300
MAX_REOFFERS = 1


def manager_for(workspace):
    from hitl.manager import HitlManager, HitlStore, default_store  # noqa: PLC0415
    return HitlManager(HitlStore(default_store(Path(workspace))))


def clicks_lock(manager):
    from file_lock import locked_file  # noqa: PLC0415
    return locked_file(Path(manager.store.root).parent / LOCK_NAME, create_mode=0o600)


def answered_cards(manager) -> list:
    """Every question card the owner clicked that is not settled, oldest first."""
    from hitl.schema import STATUS_IN_PROGRESS  # noqa: PLC0415
    out = []
    for req in sorted(manager.active(), key=lambda r: r.created_at):
        if req.runtime != CARD_RUNTIME or req.status != STATUS_IN_PROGRESS or not req.chosen_action:
            continue
        subject = req.subject or {}
        label = next((a.label for a in req.actions if a.id == req.chosen_action), req.chosen_action)
        out.append({"hitl_id": req.id, "ask_id": subject.get("ask_id") or req.guard,
                    "action_id": req.chosen_action, "label": label,
                    "consequence": (subject.get("choices") or {}).get(req.chosen_action, ""),
                    "question": req.message, "claim": dict(subject.get("claim") or {})})
    return out


def stamp_claim(manager, hitl_id: str, claim: dict) -> bool:
    """Save the card's claim, whole, in one atomic store write under the store lock. False when
    the card is gone or terminal (retired meanwhile, e.g. by a concurrent `resolve`): not saved."""
    with manager.store.locked():
        req = manager.get(hitl_id)
        if req is None or req.terminal:
            return False
        req.subject = {**(req.subject or {}), "claim": dict(claim)}
        manager.store.save(req)
        return True


def retire_cards(manager, ask_id: str) -> list:
    """Cancel every live card of this question (clicked or not); returns their ids."""
    gone = []
    for req in manager.active():
        if req.runtime == CARD_RUNTIME and ((req.subject or {}).get("ask_id") or req.guard) == ask_id:
            manager.cancel(req.id)
            gone.append(req.id)
    return gone


def _token() -> str:
    return os.urandom(8).hex()


def _advanced(manager, c: dict, claim: dict, now: float) -> dict:
    """Credit this pass to a handed claim's lease (pending_questions_lease) and persist it."""
    lease = lease_mod.advance(claim.get("lease"), now, LEASE_MAX_STEP_SEC)
    if lease != claim.get("lease"):
        claim = {**claim, "lease": lease}
        stamp_claim(manager, c["hitl_id"], claim)
    return claim


def _retired(manager, c: dict) -> dict:
    """The entry fields for a card whose claim save was refused: re-read, never actionable."""
    req = manager.get(c["hitl_id"])
    status = req.status if req is not None else "gone"
    return {"actionable": False, "retired": True,
            "close_message": f"not handed over: the card was settled meanwhile ({status})"}


def clicks(workspace: Path, closer, manager=None, now: Optional[float] = None) -> list:
    """One dict per unsettled click, oldest first: the choice plus `actionable` (True in the pass
    that hands it over, and once more when a handed claim outlived its deadline unacked:
    `reoffered`), `needs_attention` (unacked past the deadline after that single re-offer),
    `token` (what `ack_click` must carry), `stale`, `deferred`, `handed_before`, `closed` and
    `close_message`. `closer(ws, ask_id, token)` returns (closed | stale | deferred,
    message)."""
    ws = Path(workspace)
    manager = manager or manager_for(ws)
    clock = time.time if now is None else (lambda: now)  # sampled at each transition, inside the lock
    out = []
    with clicks_lock(manager):
        for c in answered_cards(manager):
            claim = c.pop("claim")
            handed = claim.get("state") == HANDED
            entry = {**c, "actionable": False, "reoffered": False, "needs_attention": False, "stale": False,
                     "deferred": False, "handed_before": handed, "closed": handed, "token": None,
                     "close_message": "closed earlier by this click"}
            if handed:
                claim = _advanced(manager, c, claim, clock())
                if lease_mod.expired(claim.get("lease"), HANDED_LEASE_SEC):
                    if int(claim.get("reoffers") or 0) < MAX_REOFFERS:
                        claim = {**claim, "lease": lease_mod.new(clock()), "handed_at": clock(), "token": _token(),
                                 "reoffers": int(claim.get("reoffers") or 0) + 1}
                        if stamp_claim(manager, c["hitl_id"], claim):
                            entry.update(actionable=True, reoffered=True, token=claim["token"])
                        else:
                            entry.update(_retired(manager, c))
                    else:
                        entry.update(needs_attention=True, token=claim.get("token"))
                out.append(entry)
                continue
            if not claim.get("token"):
                claim = {"token": _token(), "state": CLAIMING}
                if not stamp_claim(manager, c["hitl_id"], claim):
                    entry.update(_retired(manager, c))
                    out.append(entry)
                    continue
            outcome, entry["close_message"] = closer(ws, c["ask_id"], claim["token"])
            if outcome == CLOSED:
                if stamp_claim(manager, c["hitl_id"], {**claim, "state": HANDED, "handed_at": clock(),
                                                       "lease": lease_mod.new(clock())}):
                    entry.update(actionable=True, closed=True, token=claim["token"])
                else:
                    entry.update(_retired(manager, c), closed=True)
            elif outcome == STALE:
                manager.cancel(c["hitl_id"])
                entry["stale"] = True
            else:
                entry["deferred"] = True  # the claim stays `claiming`, to be retried
            out.append(entry)
    return out


def due_clicks(workspace: Path, manager=None, now: Optional[float] = None) -> list:
    """Ask ids a `clicks` pass would act on, or that need the owner's attention: a click never
    processed, a claim left `claiming` (deferred, or its pass died), or a handed claim whose lease
    expired (to be re-offered, or listed NEEDS ATTENTION once its re-offer is used). Its only write
    is crediting this pass to each handed lease, under the clicks lock, so a host whose clicks pass
    runs only on demand still ages its leases on the health check's cadence."""
    manager = manager or manager_for(workspace)
    clock = time.time if now is None else (lambda: now)
    out = []
    with clicks_lock(manager):
        for c in answered_cards(manager):
            claim = c["claim"]
            if claim.get("state") != HANDED:
                out.append(c["ask_id"])
            elif lease_mod.expired(_advanced(manager, c, claim, clock()).get("lease"), HANDED_LEASE_SEC):
                out.append(c["ask_id"])
    return out


def ack_click(workspace: Path, ask_id: str, token: str, manager=None) -> tuple:
    """(done, message): the consumer that holds this question's handed choice acted on it. The
    token must be the claim's current one: after a re-offer, the earlier consumer's ack is stale."""
    manager = manager or manager_for(workspace)
    with clicks_lock(manager):
        for c in answered_cards(manager):
            claim = c["claim"]
            if c["ask_id"] != ask_id or claim.get("state") != HANDED:
                continue
            if claim.get("token") != token:
                return False, (f"stale ack for {ask_id}: that choice was re-offered to another consumer "
                               "after its lease ran out; nothing changed")
            manager.resolve(c["hitl_id"])
            return True, f"acknowledged {ask_id} ({c['hitl_id']})"
    return False, f"no handed-out click for {ask_id} to acknowledge"
