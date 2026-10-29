"""Owner decision cards: one builder for a choice request and one store handle per workspace.

Every producer of a button card for the owner (report-feedback, the worker pool, pending
questions) builds its HumanRequirement here, so the action list, the turn-on-click default
and the store location are one policy; the projector and the reply handler do the rest.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .manager import HitlManager, HitlStore, default_store
from .schema import Action, HumanRequirement

CHOICE_KIND = "choice"
ACTION_KIND = "confirmation"


def manager_for(workspace: Optional[Path] = None) -> HitlManager:
    """The workspace's HITL store: the bridge projects what is created here and applies the clicks."""
    return HitlManager(HitlStore(default_store(Path(workspace) if workspace is not None else None)))


def new_id() -> str:
    """A requirement id allocated before the card is stored, so a record can name it first."""
    return f"hitl_{uuid.uuid4().hex[:12]}"


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
                   kind: str = CHOICE_KIND, turn_on_action: bool = True,
                   req_id: Optional[str] = None) -> HumanRequirement:
    """One button per action. `guard` keys the card to the producer's own record, so the
    store dedupes a repeat; with `turn_on_action` the applied click reaches the core as a task."""
    acts = [as_action(a) for a in actions]
    if not acts:
        raise ValueError("a choice card needs at least one action")
    if len({a.id for a in acts}) != len(acts):
        raise ValueError("action ids must be unique on one card")
    req = HumanRequirement(kind=kind, runtime=runtime, message=question, title=title, guard=guard,
                           device=device, actions=acts, subject=dict(subject or {}),
                           turn_on_action=turn_on_action)
    if req_id is not None:
        req.id = req_id
    return req


def create_choice(question: str, actions: Iterable[Any], guard: str, subject: Dict[str, Any], *,
                  runtime: str, title: str = "", device: Optional[Dict[str, str]] = None,
                  kind: str = CHOICE_KIND, turn_on_action: bool = True,
                  manager: Optional[HitlManager] = None,
                  workspace: Optional[Path] = None) -> HumanRequirement:
    """Build and store the card; returns the stored requirement (an existing one on a repeat)."""
    req = choice_request(question, actions, guard, subject, runtime=runtime, title=title,
                         device=device, kind=kind, turn_on_action=turn_on_action)
    return (manager or manager_for(workspace)).create(req)
