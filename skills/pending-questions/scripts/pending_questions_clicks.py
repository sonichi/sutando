"""The owner's decision-card clicks on pending questions, handed to exactly one consumer.

A card for a question is a HITL `choice` requirement with runtime `pending-questions`, guard and
`subject.ask_id` set to the ask id, and `subject.choices` = {action id: consequence}. The bridge
records a click (`chosen_action`) and forwards it as a `hitl_click` task; `clicks` turns each
recorded click into at most one actionable choice:

- Every pass runs under one exclusive lock beside the HITL store, so the lock's holder is the only
  live claimant. A claim still marked `claiming` was left by a pass that died before it handed the
  choice over (its lock is free now): it is re-offered.
- The pass claims the click (a token, saved atomically), closes the row as Answered in one guarded
  write that also requires the owner's Status to be open, then records the claim `handed` BEFORE it
  returns the choice. Later passes report a handed claim as "handed out, awaiting ack", never as a
  new choice; only `ack_click` settles it (or marks it acknowledged while its close is retried).
- A row the owner (or code without this click's claim) already closed makes the click stale: its
  card is cancelled and nothing is offered.
"""
from __future__ import annotations

import os
from pathlib import Path

CARD_RUNTIME = "pending-questions"
CLAIMING, HANDED, ACKED = "claiming", "handed", "acked"
LOCK_NAME = "pending-questions-clicks.lock"


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


def stamp_claim(manager, hitl_id: str, claim: dict) -> None:
    """Save the card's claim, whole, in one atomic store write under the store lock."""
    with manager.store.locked():
        req = manager.get(hitl_id)
        if req is not None and not req.terminal:
            req.subject = {**(req.subject or {}), "claim": dict(claim)}
            manager.store.save(req)


def retire_cards(manager, ask_id: str) -> list:
    """Cancel every live card of this question (clicked or not); returns their ids."""
    gone = []
    for req in manager.active():
        if req.runtime == CARD_RUNTIME and ((req.subject or {}).get("ask_id") or req.guard) == ask_id:
            manager.cancel(req.id)
            gone.append(req.id)
    return gone


def clicks(workspace: Path, closer, refused_prefix: str, code_closed_prefix: str, manager=None) -> list:
    """One dict per unsettled click, oldest first: the choice plus `actionable` (True exactly once
    per click, in the pass that hands it over), `stale`, `handed_before`, `acked_before`,
    `closed` and `close_message`. `closer(ws, ask_id, "Answered")` closes the row requiring an
    open owner Status, returning (ok, message) with the adapter's refusal prefixes."""
    ws = Path(workspace)
    manager = manager or manager_for(ws)
    out = []
    with clicks_lock(manager):
        for c in answered_cards(manager):
            claim = c.pop("claim")
            state = claim.get("state")
            entry = {**c, "actionable": False, "stale": False, "handed_before": state == HANDED,
                     "acked_before": state == ACKED, "closed": bool(claim.get("row_closed")),
                     "close_message": ""}
            if state in (HANDED, ACKED) and claim.get("row_closed"):
                entry["close_message"] = "closed; handed out earlier and not acknowledged"
                out.append(entry)
                continue
            earlier = bool(claim)
            if state not in (HANDED, ACKED):
                claim = {"token": os.urandom(8).hex(), "state": CLAIMING}
                stamp_claim(manager, c["hitl_id"], claim)
            ok, msg = closer(ws, c["ask_id"], "Answered")
            closed_by_us = not ok and earlier and msg.startswith(code_closed_prefix)
            entry["close_message"] = msg
            if not ok and not closed_by_us and msg.startswith(refused_prefix):
                manager.cancel(c["hitl_id"])
                entry["stale"] = True
                out.append(entry)
                continue
            entry["closed"] = ok or closed_by_us
            if state == ACKED:
                if entry["closed"]:
                    manager.resolve(c["hitl_id"])
                out.append(entry)
                continue
            if state != HANDED:
                stamp_claim(manager, c["hitl_id"], {**claim, "state": HANDED, "row_closed": entry["closed"]})
                entry["actionable"] = True
            elif entry["closed"]:
                stamp_claim(manager, c["hitl_id"], {**claim, "row_closed": True})
            out.append(entry)
    return out


def ack_click(workspace: Path, ask_id: str, manager=None) -> tuple:
    """(done, message): the consumer acted on this question's handed choice. A closed row settles
    the card; one still open is marked acknowledged, so later passes only retry its close."""
    manager = manager or manager_for(workspace)
    done = []
    with clicks_lock(manager):
        for c in answered_cards(manager):
            claim = c["claim"]
            if c["ask_id"] != ask_id or claim.get("state") != HANDED:
                continue
            if claim.get("row_closed"):
                manager.resolve(c["hitl_id"])
            else:
                stamp_claim(manager, c["hitl_id"], {**claim, "state": ACKED})
            done.append(c["hitl_id"])
    if not done:
        return False, f"no handed-out click for {ask_id} to acknowledge"
    return True, f"acknowledged {ask_id} ({', '.join(done)})"
