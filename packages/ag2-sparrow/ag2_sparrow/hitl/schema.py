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

from .host import device_host

WIRE_FIELD = "space.ag2.hitl"
# Follow-up notice tag: which cards a reminder or recovery message is about.
NOTICE_FIELD = "space.ag2.hitl_notice"
# The subject fields a card may show. The rest of `subject` is the producer's own bookkeeping
# (claims, tokens, leases, session keys) and never leaves the host.
WIRE_SUBJECT_FIELDS = ("tool", "input", "options", "choices")
# A recovery card also shows its tap's receipt outcome (hitl.recovery), so a client knows the answer.
WIRE_RECEIPT_OUTCOMES = ("done", "refused", "stale", "failed")
# What came of the owner's answer (the card's `outcome`): applied now, then done or failed. `closed`
# is neutral: the card closed before verification saw a result, so it is neither success nor failure.
OUTCOME_RUNNING, OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_CLOSED = "running", "done", "failed", "closed"
OUTCOME_STATES = (OUTCOME_RUNNING, OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_CLOSED)
OUTCOME_TEXT_MAX = 200
RECEIPT_FAILED_TEXT = {
    "refused": "It was refused, so nothing was done.",
    "stale": "The card changed before your answer arrived, so nothing was done.",
    "failed": "It could not be done.",
}
SUPERSEDED_TEXT = "It is still blocked. A new card follows."
CLOSED_UNVERIFIED_TEXT = "Closed without verification."
# What verification saw after a done recovery action that counts as done: the session working again.
# Still blocked, a failed restart, or up with its waiting work not seen starting is not success.
VERIFY_SEEN_DONE = ("working",)
# What verification saw that is no evidence either way (a check window or a restart's deadline ran
# out unseen): the card closes neutral, never as failed. Failed is only a failure seen.
VERIFY_SEEN_UNKNOWN = ("up", "unknown")


# Canonical JSON's integer range; a number outside it is refused like a float.
WIRE_INT_MAX = 2 ** 53 - 1


def wire_safe(value: Any) -> Any:
    """`value` with only canonical-JSON numbers at any depth: an integral float within
    +-WIRE_INT_MAX becomes an int, any other float or out-of-range int its repr string."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value.is_integer() and -WIRE_INT_MAX <= value <= WIRE_INT_MAX:
            return int(value)
        return repr(value)
    if isinstance(value, int):
        return value if -WIRE_INT_MAX <= value <= WIRE_INT_MAX else repr(value)
    if isinstance(value, dict):
        return {k: wire_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire_safe(v) for v in value]
    return value

KINDS = frozenset(
    {"auth", "permission", "choice", "confirmation", "billing", "external_action",
     "core-blocked", "unknown"}
)

CATEGORY_BLOCKED = "blocked"
CATEGORY_DECISION = "decision"
# Mirrors ag2-space/cinny-webclient src/app/components/message/hitlCategory.ts
# (#847). An unlisted kind is BLOCKED: under-stating a block strands the user.
_KIND_CATEGORY = {
    "auth": CATEGORY_BLOCKED,
    "permission": CATEGORY_BLOCKED,
    "billing": CATEGORY_BLOCKED,
    "external_action": CATEGORY_BLOCKED,
    "choice": CATEGORY_DECISION,
    "confirmation": CATEGORY_DECISION,
}


def category_of(kind: "str | None") -> str:
    """Which of the two presentations this kind gets."""
    return _KIND_CATEGORY.get(kind or "", CATEGORY_BLOCKED)

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
    # The applied click also reaches the core as a task, so an executor that runs
    # at the end of an agent turn (a Stop hook) runs now, not after an unrelated turn.
    turn_on_action: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # The room the card is for; when set, a reply from any other room is stale.
    room: str = ""
    # The producer's own word on what came of the answer ({state, text}); see wire_outcome.
    outcome: Optional[Dict[str, str]] = None

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

    def set_outcome(self, state: str, text: str = "") -> None:
        """Record what came of the owner's answer; it outranks what the status alone would say."""
        if state not in OUTCOME_STATES:
            raise ValueError(f"unknown outcome state: {state!r}")
        self.outcome = outcome_wire(state, text)

    def wire_outcome(self) -> Optional[Dict[str, str]]:
        """The card's `outcome`, or None while it still asks or nobody answered it. The producer's
        own outcome wins, then a recovery receipt, then the status (in_progress runs, resolved is done).
        A done receipt is only dispatched: it runs until verification has seen what came of it, and
        a card closed before then is `closed`, never done; failed is only what verification saw."""
        if self.status == STATUS_PENDING or not self.chosen_action:
            return None
        own = self.outcome
        if isinstance(own, dict) and own.get("state") in OUTCOME_STATES:
            return outcome_wire(own["state"], own.get("text"))
        subject = self.subject or {}
        receipt = subject.get("receipt")
        if isinstance(receipt, dict) and receipt.get("outcome") in WIRE_RECEIPT_OUTCOMES:
            text = receipt.get("text")
            check = subject.get("verify")
            seen = check.get("seen") if isinstance(check, dict) else None
            if seen in VERIFY_SEEN_UNKNOWN:
                return outcome_wire(OUTCOME_CLOSED, text or CLOSED_UNVERIFIED_TEXT)
            if receipt["outcome"] != "done":
                return outcome_wire(OUTCOME_FAILED, text or RECEIPT_FAILED_TEXT[receipt["outcome"]])
            if subject.get("superseded") is True:
                return outcome_wire(OUTCOME_FAILED, text or SUPERSEDED_TEXT)
            if isinstance(check, dict) and not seen:
                # Still verifying (or a fallback restart runs), with no deadline; closed first is unknown.
                if self.terminal:
                    return outcome_wire(OUTCOME_CLOSED, CLOSED_UNVERIFIED_TEXT)
                return outcome_wire(OUTCOME_RUNNING)
            if seen in VERIFY_SEEN_DONE or not isinstance(check, dict):
                return outcome_wire(OUTCOME_DONE, text)
            return outcome_wire(OUTCOME_FAILED, text or SUPERSEDED_TEXT)
        if self.status == STATUS_IN_PROGRESS:
            return outcome_wire(OUTCOME_RUNNING)
        if self.status == STATUS_RESOLVED:
            return outcome_wire(OUTCOME_DONE)
        return None  # cancelled or expired: the status says it

    def wire_device(self) -> Optional[Dict[str, str]]:
        """The device as a reader sees it: the producer's dict plus `host` (this
        machine's label) when the producer left it out. Every card, one place.
        Assumes the store is per-host: `state/` is not carried by default, so the
        serializing machine is the terminal's. A `vault.sync.include` covering
        `state/` would break that; a producer that knows a remote host must set
        `host` itself."""
        if not self.device:
            return None
        device = dict(self.device)
        if not device.get("host"):
            host = device_host()
            if host:
                device["host"] = host
        return device

    def wire_outcome(self) -> Optional[Dict[str, str]]:
        """What came of the human's answer, for the card's result line (the client reads `running`,
        `done`, `failed` and neutral `closed`): `running` once an answer was taken and is being applied;
        `done` once the driver that sent it saw the runtime carry on (`subject.driven_settled`);
        `failed` when a driver gave it up, with the driver's own words (`subject.driver_note`, never
        `answer`, which holds what the human submitted); `closed` when a driver sent the answer
        (`subject.driven_at`) and the card closed with no such evidence, which is never read as success.
        None for a card nobody answered, and for a closed card no driver handled: its status says it."""
        if not self.chosen_action or self.decided_by == "policy":
            return None
        subject = self.subject or {}
        if self.status == STATUS_IN_PROGRESS:
            return dict(state="running")
        note = subject.get("driver_note")
        if self.status == STATUS_EXPIRED and isinstance(note, str) and note.strip():
            return dict(state="failed", text=" ".join(note.split())[:200])
        if self.status == STATUS_RESOLVED and subject.get("driven_settled"):
            return dict(state="done")
        if self.terminal and subject.get("driven_at"):
            return dict(state="closed")
        return None

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
        device = self.wire_device()
        if device:
            wire["device"] = device
        if self.actions:
            wire["actions"] = [a.to_wire() for a in self.actions]
        subject = {k: self.subject[k] for k in WIRE_SUBJECT_FIELDS if k in (self.subject or {})}
        receipt = (self.subject or {}).get("receipt")
        if isinstance(receipt, dict) and receipt.get("outcome") in WIRE_RECEIPT_OUTCOMES:
            # A recovery tap's answer: its outcome and time only, the rest of the receipt stays on the host.
            at = receipt.get("at")
            subject["receipt"] = {"outcome": receipt["outcome"],
                                  **({"at": at} if isinstance(at, int) and not isinstance(at, bool)
                                     and 0 <= at <= WIRE_INT_MAX else {})}
        if (self.subject or {}).get("superseded") is True:
            subject["superseded"] = True  # closed for a fresh card: a done receipt here is not success
        if subject:
            wire["subject"] = subject
        outcome = self.wire_outcome()
        if outcome:
            wire["outcome"] = outcome
        if self.expires_at is not None:
            wire["expires_at"] = self.expires_at
        if self.turn_on_action:
            wire["turn_on_action"] = True
        outcome = self.wire_outcome()
        if outcome:
            wire["outcome"] = outcome
        # `answer` is inbound-only (what the human typed back); a card never
        # renders it, so it is persisted but deliberately not on the wire.
        return wire_safe(wire)


def outcome_wire(state: str, text: Optional[str] = None) -> Dict[str, str]:
    """{state, text?}: the text as one line, cut at a word to OUTCOME_TEXT_MAX."""
    line = " ".join(str(text or "").split())
    if len(line) > OUTCOME_TEXT_MAX:
        line = line[:OUTCOME_TEXT_MAX - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return {"state": state, **({"text": line} if line else {})}


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
    # Where the reply came from and who sent it, as the transport attests them.
    room: str = ""
    sender: str = ""

    @classmethod
    def from_wire(cls, payload: Dict[str, Any]) -> "ActionReply":
        try:
            return cls(
                hitl_id=str(payload["hitl_id"]),
                expected_revision=int(payload["expected_revision"]),
                action_id=str(payload["action_id"]),
                guard=str(payload.get("guard", "")),
                answer=payload.get("answer"),
                room=str(payload.get("room") or ""),
                sender=str(payload.get("sender") or ""),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise MalformedActionError(f"bad action payload: {e}") from e


class MalformedActionError(Exception):
    pass


def validate_action(req: HumanRequirement, reply: ActionReply) -> Action:
    """The three-layer stale gate. Returns the matched Action or raises.

    expected_revision guards the HITL object the user saw; guard guards the
    live runtime interaction beneath it; room, when the requirement names one,
    guards where the click may come from. Any mismatch -> STALE: the driver
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
    if req.room and reply.room != req.room:
        raise StaleRequirementError(f"{req.id}: reply from another room")
    for action in req.actions:
        if action.id == reply.action_id:
            return action
    raise MalformedActionError(f"{req.id}: unknown action id {reply.action_id!r}")
