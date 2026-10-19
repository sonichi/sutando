"""The one accepter for a claude-local card: bridge clicks, the owner-DM `/exit` and the local
`stop --confirm` all land here. The bridge owns it, so it lives outside the adapter.

Lock order, always: the progress gate (`<root>/state/progress.lock`) -> the supervisor lock (the
install's `claude-local.supervisor.lock`) -> one fresh HitlStore's `locked()`, the manager
constructed over that same object. The work root and the supervisor lock come from the opaque install
id on the requirement, resolved through what the adapter registered beside the store; the
root-relative coordinates must resolve under that root to its own progress gate and supervisor
record, and those resolved paths are the gate opened and the record read. Under all three locks the requirement is
reloaded, the coordinates and the live stall token revalidated, and `apply_action(reply, patch)`
writes the chosen action, the revision and status, the saga key and the effect phase in its one save.
A directory fsync that fails after the replace is `commit-unknown`: nothing else is recorded, no
effect runs, and reconciliation re-fsyncs and re-saves the identical record before any effect.
Progress invalidates every action.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

from . import claude_local_card as card, claude_local_records as records
from .hitl.schema import (ActionReply, HumanRequirement, MalformedActionError, STATUS_IN_PROGRESS, STATUS_PENDING,
                          StaleRequirementError)

_OUTER = threading.local()


class Refused(Exception):
    """The action was not applied; `why` is the one line for the caller."""

    def __init__(self, why: str, outcome: str = "rejected"):
        super().__init__(why)
        self.why, self.outcome = why, outcome


def _locate(reply_id: str, state_dir) -> "tuple[card.HitlStore, card.HitlManager, HumanRequirement]":
    """Exactly one store match: the claude-local root holds the id and the default store does not;
    zero or two matches refuse before any lock, with nothing touched."""
    if not reply_id.startswith(card.HITL_ID_PREFIX):
        raise Refused("%s is not a claude-local requirement id" % reply_id)
    store, mgr = card.manager(card.store_root(state_dir))
    found = store.load(reply_id)
    default = card.HitlStore(card.default_root(state_dir)) if card.default_root(state_dir).is_dir() else None
    if default is not None and default.load(reply_id) is not None:
        raise Refused("%s exists in both stores; ambiguous, nothing applied" % reply_id)
    if found is None:
        raise Refused("no claude-local requirement %s" % reply_id)
    return store, mgr, found


def coordinates(root, sub: dict) -> "tuple":
    """(progress gate, supervisor record): the card's coordinates resolved under the root, which must
    name this root's own gate and record (the files the Mod and the launcher lock); else ValueError."""
    if not card.coordinates_ok(sub):
        raise ValueError("lock coordinates are not root-relative and confined")
    gate, record = card.resolve_under(root, sub["progress_lock"]), card.resolve_under(root, sub["supervisor_record"])
    if (gate, record) != (records.progress_lock_path(root).resolve(), records.supervisor_path(root).resolve()):
        raise ValueError("lock coordinates do not name this root's progress gate and supervisor record")
    return gate, record


@contextmanager
def locks(root, supervisor_lock, store, gate=None):
    """progress gate -> supervisor lock -> the store's own lock, in that order and no other."""
    gate = records.progress_lock_path(root) if gate is None else gate
    with records.flocked(gate), records.flocked(supervisor_lock), store.locked():
        _OUTER.held = getattr(_OUTER, "held", 0) + 1
        try:
            yield
        finally:
            _OUTER.held -= 1


def _apply(mgr, store, reply, patch):
    """apply_action only from inside `locks`, and only on the manager over the outer store."""
    if not getattr(_OUTER, "held", 0):
        raise Refused("apply_action entered without the progress gate and the supervisor lock; refused")
    same_store(mgr, store)
    return mgr.apply_action(reply, patch)


def same_store(mgr, store) -> None:
    """The manager must be constructed over the outer store object: a check `python -O` keeps."""
    if mgr.store is not store:
        raise Refused("apply_action on a manager over another store object; refused")


def live_token(root, record=None) -> str:
    return records.stall_token(records.load_supervisor(root, record).get("stall"))


def _revalidate(req: HumanRequirement, reply: ActionReply, root, owner_ok: bool, local: bool, opened) -> dict:
    """`opened`: the (gate, record) taken from the card before the locks; the reloaded card must name them."""
    sub = card.local(req)
    if not owner_ok:
        raise Refused("%s: only the owner may act on this card" % req.id)
    try:
        if coordinates(root, sub) != opened:
            raise ValueError("lock coordinates changed under the locks")
    except ValueError as e:
        raise Refused("%s: %s" % (req.id, e))
    room = sub.get("room") or req.room
    if not local and (not room or reply.room != room):
        raise Refused("%s: reply from another room (or the card has no projected room yet)" % req.id)
    supervisor = records.load_supervisor(root, opened[1])
    if sub.get("install_id") != supervisor.get("install_id"):
        raise Refused("%s: the card names another install" % req.id)
    if sub.get("stall_token", "") != live_token(root, opened[1]):
        raise Refused("%s: the session made progress since the card; action cancelled" % req.id, "cancelled")
    return sub


def reconcile_unconfirmed(store, mgr, req: HumanRequirement) -> bool:
    """An in_progress requirement with a recorded effect phase and no effect evidence is
    recorded-unconfirmed: re-fsync the store directory and re-save the identical record, so the
    acceptance is durable before any effect. True when such a record was reconciled."""
    sub = card.local(req)
    if req.status != STATUS_IN_PROGRESS or not sub.get("effect_phase") or sub.get("effect_evidence"):
        return False
    records.fsync_dir(store.root)
    store.save(req, store.projection(req.id))
    return True


def accept(reply: ActionReply, *, state_dir, owner_ok: bool, local: bool = False, log=print) -> str:
    """applied | applied:reconciled | applied:rearm-pending:<why> | rejected:<why> | cancelled:<why> |
    commit-unknown:<why>. `local`
    is the machine's own `stop --confirm` (no room); every other reply must come from the card's room."""
    try:
        store, mgr, found = _locate(reply.hitl_id, state_dir)
        root, supervisor_lock = card.resolve_install(store.root, card.local(found).get("install_id"))
        opened = coordinates(root, card.local(found))
    except Refused as e:
        return "%s:%s" % (e.outcome, e.why)
    except ValueError as e:
        return "rejected:%s" % e
    try:
        with locks(root, supervisor_lock, store, gate=opened[0]):
            req = store.load(reply.hitl_id)
            if req is None:
                return "rejected:%s vanished" % reply.hitl_id
            sub = _revalidate(req, reply, root, owner_ok, local, opened)
            if req.status == STATUS_IN_PROGRESS and reconcile_unconfirmed(store, mgr, req):
                return "applied:reconciled"
            if req.status != STATUS_PENDING:
                raise Refused("%s already %s" % (req.id, req.status))
            if reply.action_id == card.ACTION_KEEP and sub.get("saga_key"):
                raise Refused("%s already rearmed" % req.id)
            patch = card.patch_for(reply.action_id, reply.expected_revision, sub.get("incident") or "")
            try:
                action = _apply(mgr, store, reply, patch)
            except OSError as e:
                return "commit-unknown:%s" % (e.strerror or e)
            if action.id == card.ACTION_KEEP:
                try:
                    card.reconcile_rearm(store, mgr, store.load(req.id))
                except ValueError as e:     # the decision is committed; the launcher's reconcile finishes it
                    log("claude-local: %s -> keep waiting recorded; rearm pending: %s" % (req.id, e))
                    return "applied:rearm-pending:%s" % e
            log("claude-local: %s -> %s accepted (revision %d)" % (req.id, action.id, reply.expected_revision))
            return "applied"
    except Refused as e:
        return "%s:%s" % (e.outcome, e.why)
    except (StaleRequirementError, MalformedActionError, ValueError) as e:
        return "rejected:%s" % e


def local_stop(state_dir, opaque: str, log=print) -> str:
    """`stop --confirm`: this install's open requirement's Stop, applied in-process through the same
    accepter; more than one open refuses."""
    store, _ = card.manager(card.store_root(state_dir))
    opened = [r for r in card.open_actions(store) if card.local(r).get("install_id") == opaque]
    if not opened:
        return "rejected:no stall card is open; attach and type /exit yourself"
    if len(opened) > 1:
        return "rejected:%d stall cards are open; use the card in the owner DM, or attach and type /exit" % len(opened)
    req = opened[0]
    reply = ActionReply(hitl_id=req.id, expected_revision=req.revision, action_id=card.ACTION_STOP, guard=req.guard,
                        room=card.local(req).get("room") or req.room, sender="local")
    return accept(reply, state_dir=state_dir, owner_ok=True, local=True, log=log)


def exit_text_reply(store, room: str, sender: str) -> "ActionReply | int":
    """The owner's bare `/exit`: correlated to the one open claude-local action in this room; else
    the count of open actions (0: the text stays a message; more than one: refused, never a guess)."""
    opened = [r for r in card.open_actions(store) if (card.local(r).get("room") or r.room) == room]
    if len(opened) != 1:
        return len(opened)
    req = opened[0]
    return ActionReply(hitl_id=req.id, expected_revision=req.revision, action_id=card.ACTION_STOP, guard=req.guard,
                       room=room, sender=sender)
