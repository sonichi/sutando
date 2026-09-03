"""HITL v1 domain model + wire contract (space.ag2.hitl).

Layering: RuntimeEvent -> HumanRequirement (this module, domain model)
-> space.ag2.hitl (wire contract, `to_wire()`) -> RequirementCard (UI).
State here is authority; every Matrix event is a projection of one revision.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

WIRE_FIELD = "space.ag2.hitl"

KINDS = frozenset(
    {"auth", "permission", "choice", "confirmation", "billing", "external_action", "unknown"}
)

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_RESOLVED = "resolved"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
TERMINAL_STATUSES = frozenset({STATUS_RESOLVED, STATUS_CANCELLED, STATUS_EXPIRED})
# pending may resolve directly (a readiness probe can clear a requirement the
# user fixed out-of-band, with no action click ever arriving).
LEGAL_TRANSITIONS = {
    STATUS_PENDING: frozenset({STATUS_IN_PROGRESS, *TERMINAL_STATUSES}),
    STATUS_IN_PROGRESS: frozenset(TERMINAL_STATUSES),
}


@dataclass
class Action:
    id: str
    kind: str
    label: str

    def to_wire(self) -> Dict[str, str]:
        return {"id": self.id, "kind": self.kind, "label": self.label}


@dataclass
class HumanRequirement:
    kind: str
    runtime: str
    message: str
    id: str = field(default_factory=lambda: f"hitl_{uuid.uuid4().hex[:12]}")
    status: str = STATUS_PENDING
    revision: int = 1
    # Opaque driver token for the live runtime interaction beneath the
    # requirement (TUI screen hash, ACP request id). Never interpreted here.
    guard: str = ""
    device: Optional[Dict[str, str]] = None
    title: str = ""
    actions: List[Action] = field(default_factory=list)
    # Task ids blocked on this requirement; resumed when it resolves.
    blocked_task_ids: List[str] = field(default_factory=list)
    # The action id a human chose (set on apply_action) — what a blocking
    # hook driver reads to turn a card click into its permissionDecision.
    chosen_action: Optional[str] = None
    # What is being asked, structurally (e.g. {"tool": "Bash", "input": "..."}):
    # the policy reads this; the message stays the human rendering.
    subject: Dict[str, Any] = field(default_factory=dict)
    # "policy" when the Manager auto-answered it: never projected as a card.
    decided_by: Optional[str] = None
    # A payload beyond the chosen action (free text, multi-select labels): set by
    # apply_action from ActionReply.answer; None for pure button clicks.
    answer: Optional[Any] = None
    # Absolute epoch after which the producer treats the requirement as expired.
    expires_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            self.kind = "unknown"

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def transition(self, new_status: str, guard: Optional[str] = None) -> None:
        """Move to new_status, bumping revision. Raises on an illegal move."""
        allowed = LEGAL_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise StaleRequirementError(
                f"illegal transition {self.status} -> {new_status} for {self.id}"
            )
        self.status = new_status
        if guard is not None:
            self.guard = guard
        self.revision += 1
        self.updated_at = time.time()

    def refresh_guard(self, guard: str) -> None:
        """The underlying runtime interaction changed (e.g. TUI repaint):
        new guard, new revision, same status. Old cards go stale."""
        if self.terminal:
            raise StaleRequirementError(f"{self.id} is terminal ({self.status})")
        self.guard = guard
        self.revision += 1
        self.updated_at = time.time()

    def to_wire(self) -> Dict[str, Any]:
        wire: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "runtime": self.runtime,
            "status": self.status,
            "revision": self.revision,
            "guard": self.guard,
            "message": self.message,
        }
        if self.title:
            wire["title"] = self.title
        if self.device:
            wire["device"] = dict(self.device)
        if self.actions:
            wire["actions"] = [a.to_wire() for a in self.actions]
        if self.subject:
            wire["subject"] = dict(self.subject)
        if self.expires_at is not None:
            wire["expires_at"] = self.expires_at
        # `answer` is inbound-only (what the human typed back); a card never
        # renders it, so it is persisted but deliberately not on the wire.
        return wire


class StaleRequirementError(Exception):
    """The action or transition refers to a version of the requirement (or of
    the runtime interaction beneath it) that no longer exists."""


@dataclass
class ActionReply:
    """Wire shape of a user action: {hitl_id, expected_revision, action_id, guard}."""

    hitl_id: str
    expected_revision: int
    action_id: str
    guard: str
    # Optional payload for actions that carry more than a click: free text, a
    # list of selected labels. Stored on the requirement as `answer`.
    answer: Optional[Any] = None

    @classmethod
    def from_wire(cls, payload: Dict[str, Any]) -> "ActionReply":
        try:
            return cls(
                hitl_id=str(payload["hitl_id"]),
                expected_revision=int(payload["expected_revision"]),
                action_id=str(payload["action_id"]),
                guard=str(payload.get("guard", "")),
                answer=payload.get("answer"),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise MalformedActionError(f"bad action payload: {e}") from e


class MalformedActionError(Exception):
    pass


def validate_action(req: HumanRequirement, reply: ActionReply) -> Action:
    """The two-layer stale gate. Returns the matched Action or raises.

    expected_revision guards the HITL object the user saw; guard guards the
    live runtime interaction beneath it. Either mismatch -> STALE: the driver
    must never deliver a keystroke/response for an interaction that moved.
    """
    if reply.hitl_id != req.id:
        raise MalformedActionError(f"action for {reply.hitl_id}, requirement is {req.id}")
    if req.terminal:
        raise StaleRequirementError(f"{req.id} already {req.status}")
    if reply.expected_revision != req.revision:
        raise StaleRequirementError(
            f"{req.id}: action against revision {reply.expected_revision}, now {req.revision}"
        )
    if reply.guard != req.guard:
        raise StaleRequirementError(f"{req.id}: guard mismatch (interaction changed)")
    for action in req.actions:
        if action.id == reply.action_id:
            return action
    raise MalformedActionError(f"{req.id}: unknown action id {reply.action_id!r}")
