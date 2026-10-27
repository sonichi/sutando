"""Owner decision cards: one builder for a choice request and one store handle per workspace.

Every producer of a button card for the owner (report-feedback, the worker pool, pending
questions) builds its HumanRequirement here, so the action list, the turn-on-click default
and the store location are one policy; the projector and the reply handler do the rest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .manager import HitlManager, HitlStore, default_store
from .schema import Action, HumanRequirement

CHOICE_KIND = "choice"
ACTION_KIND = "confirmation"


def manager_for(workspace: Optional[Path] = None) -> HitlManager:
    """The workspace's HITL store: the bridge projects what is created here and applies the clicks."""
    return HitlManager(HitlStore(default_store(Path(workspace) if workspace is not None else None)))


def as_action(a: Any) -> Action:
    """An Action from an Action, an {id, label[, kind]} mapping or an (id, label[, kind]) pair."""
    if isinstance(a, Action):
        return a
    if isinstance(a, dict):
        return Action(id=str(a["id"]), kind=str(a.get("kind") or ACTION_KIND), label=str(a["label"]))
    a = tuple(a)
    return Action(id=str(a[0]), kind=str(a[2]) if len(a) > 2 else ACTION_KIND, label=str(a[1]))


def choice_request(question: str, actions: Iterable[Any], guard: str, subject: Dict[str, Any], *,
                   runtime: str, title: str = "", device: Optional[Dict[str, str]] = None,
                   kind: str = CHOICE_KIND, turn_on_action: bool = True) -> HumanRequirement:
    """One button per action. `guard` keys the card to the producer's own record, so the
    store dedupes a repeat; with `turn_on_action` the applied click reaches the core as a task."""
    acts = [as_action(a) for a in actions]
    if not acts:
        raise ValueError("a choice card needs at least one action")
    if len({a.id for a in acts}) != len(acts):
        raise ValueError("action ids must be unique on one card")
    return HumanRequirement(kind=kind, runtime=runtime, message=question, title=title, guard=guard,
                            device=device, actions=acts, subject=dict(subject or {}),
                            turn_on_action=turn_on_action)


def create_choice(question: str, actions: Iterable[Any], guard: str, subject: Dict[str, Any], *,
                  runtime: str, title: str = "", device: Optional[Dict[str, str]] = None,
                  kind: str = CHOICE_KIND, turn_on_action: bool = True,
                  manager: Optional[HitlManager] = None,
                  workspace: Optional[Path] = None) -> HumanRequirement:
    """Build and store the card; returns the stored requirement (an existing one on a repeat)."""
    req = choice_request(question, actions, guard, subject, runtime=runtime, title=title,
                         device=device, kind=kind, turn_on_action=turn_on_action)
    return (manager or manager_for(workspace)).create(req)


def withdraw(manager: HitlManager, req_id: str) -> bool:
    """Cancel a card; one the gateway never showed is marked current so it is never posted.
    Returns True when it was still unshown (nothing reached the owner)."""
    req = manager.get(req_id)
    if req is None:
        return False
    unshown = manager.store.projection(req_id).get("revision", 0) == 0
    manager.cancel(req_id)
    if unshown:
        cur = manager.get(req_id)
        if cur is not None:
            manager.record_projection(req_id, cur.revision, None)
    return unshown
