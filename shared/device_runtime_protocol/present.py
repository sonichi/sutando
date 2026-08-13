"""The present() envelope: shaping what a human sees and can respond to.

Symmetric with act() in protocol status, deliberately NOT in schema (owner
ruling). Distinctions that carry the design:
- `experience_id` names the persistent human-interaction object;
  `presentation_id` names ONE create/update/resolve/dismiss command against
  it; renderer-specific delivery refs (Matrix event, notification, watch
  card) are a third, lower layer.
- `expected_version` is optimistic concurrency so parallel renderers/tasks
  can't clobber each other's updates.
- Suppression is a first-class, auditable terminal disposition — a decision
  to stay quiet must never be indistinguishable from a delivery failure, or
  the relay retries it into noise.
- SECURITY BOUNDARY: an Experience response (e.g. "Approve" on the watch) is
  authenticated USER INPUT to the Policy Engine — never an approval token.
  S2 issues the digest-bound, expiring, single-use authorization after
  verifying approver identity, action digest, effects digest, device/session
  and nonce. Renderers and bridges are never the security authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .action import ActionEnvelope, action_digest, effects_digest
from .errors import ProtocolFault

EXPERIENCE_INTENTS = ("inform", "ask", "choose", "approve", "monitor")
PRESENT_OPERATIONS = ("create", "update", "resolve", "dismiss")
DISPOSITIONS = ("deliver", "update_silently", "aggregate", "suppress", "escalate")
TERMINAL_DISPOSITIONS = ("delivered", "updated", "suppressed", "aggregated",
                         "escalated", "resolved", "dismissed")

_REQUIRED_CREATE = ("presentation_id", "experience_id", "task_id", "intent")


@dataclass
class PresentEnvelope:
    presentation_id: str
    experience_id: str
    task_id: str
    operation: str
    intent: str | None = None            # required on create
    audience: dict = field(default_factory=dict)   # {subjects: [...], scope}
    content: dict = field(default_factory=dict)
    interaction: dict = field(default_factory=dict)
    delivery_policy: dict = field(default_factory=dict)
    expected_version: int | None = None  # required on update
    trace: dict = field(default_factory=dict)
    protocol_version: str = "1"

    @classmethod
    def from_dict(cls, d: dict) -> "PresentEnvelope":
        v = d.get("protocol_version")
        version = "1" if v is None else str(v)
        if version != "1":
            raise ValueError(f"unsupported protocol_version: {version!r}")
        op = d.get("operation")
        if op not in PRESENT_OPERATIONS:
            raise ValueError(f"bad present operation: {op!r}")
        if op == "create":
            missing = [k for k in _REQUIRED_CREATE if not d.get(k)]
            if missing:
                raise ValueError(f"present create missing: {', '.join(missing)}")
            if d["intent"] not in EXPERIENCE_INTENTS:
                raise ValueError(f"bad experience intent: {d['intent']!r}")
        else:
            for k in ("presentation_id", "experience_id"):
                if not d.get(k):
                    raise ValueError(f"present {op} missing: {k}")
        # S1.1: terminating an Experience is never an unconditional write —
        # update, resolve AND dismiss all carry the version they believe in.
        if op in ("update", "resolve", "dismiss"):
            ev = d.get("expected_version")
            if isinstance(ev, bool) or not isinstance(ev, int):
                raise ValueError(f"present {op} requires integer expected_version")
        dp = d.get("delivery_policy") or {}
        disp = dp.get("disposition", "deliver")
        if disp not in DISPOSITIONS:
            raise ValueError(f"bad disposition: {disp!r}")
        return cls(
            presentation_id=d["presentation_id"],
            experience_id=d["experience_id"],
            task_id=d.get("task_id", ""),
            operation=op, intent=d.get("intent"),
            audience=d.get("audience") or {},
            content=d.get("content") or {},
            interaction=d.get("interaction") or {},
            delivery_policy={**dp, "disposition": disp},
            expected_version=d.get("expected_version"),
            trace=d.get("trace") or {},
            protocol_version=version,
        )

    def to_dict(self) -> dict:
        return {
            "protocol_version": self.protocol_version,
            "presentation_id": self.presentation_id,
            "experience_id": self.experience_id,
            "task_id": self.task_id,
            "operation": self.operation,
            "intent": self.intent,
            "audience": self.audience,
            "content": self.content,
            "interaction": self.interaction,
            "delivery_policy": self.delivery_policy,
            "expected_version": self.expected_version,
            "trace": self.trace,
        }


@dataclass
class PresentResult:
    presentation_id: str
    experience_id: str
    status: str                     # completed | failed
    disposition: str | None = None  # terminal disposition when completed
    reason: str | None = None       # e.g. "no_material_change" on suppressed
    experience_version: int | None = None
    delivery_refs: list = field(default_factory=list)  # renderer-layer refs
    fault: ProtocolFault | None = None

    def __post_init__(self) -> None:
        if self.status not in ("completed", "failed"):
            raise ValueError(f"bad present status: {self.status!r}")
        if self.status == "completed" and self.disposition not in TERMINAL_DISPOSITIONS:
            raise ValueError(
                f"completed present needs a terminal disposition, got {self.disposition!r}")

    def to_dict(self) -> dict:
        d = {
            "presentation_id": self.presentation_id,
            "experience_id": self.experience_id,
            "status": self.status,
            "disposition": self.disposition,
            "experience_version": self.experience_version,
            "delivery_refs": self.delivery_refs,
        }
        if self.reason:
            d["reason"] = self.reason
        if self.fault:
            d["fault"] = self.fault.to_dict()
        return d


def build_approval_content(*, action: ActionEnvelope, summary: str,
                           effects: list[str], preview: dict,
                           choices: list[str]) -> dict:
    """The first Experience type: an approval with structured effects and
    preview, BOUND to the canonical action (S1.1 ruling): the content carries
    approval_binding = {action_digest, effects_digest} so what was displayed
    is provably what gets approved. The rendered choices produce authenticated
    INPUT — the S2 Policy Engine, not this content or its renderer, decides
    whether that input yields an execution authorization."""
    if not choices:
        raise ValueError("approval content requires choices")
    return {
        "type": "approval",
        "summary": summary,
        "effects": effects,
        "preview": preview,
        "choices": choices,
        "approval_binding": {
            "action_digest": action_digest(action),
            "effects_digest": effects_digest(effects),
        },
    }


_RESPONSE_REQUIRED = ("response_id", "experience_id", "expected_version",
                      "subject", "surface_id", "choice", "nonce")


@dataclass
class ExperienceResponseEnvelope:
    """A human's answer to an Experience. The payload subject is a CLAIM; the
    transport supplies the authenticated principal and the dispatcher must
    verify the two agree (or overwrite from the trusted context). This
    envelope is still only input — S2 issues authorization."""

    response_id: str
    experience_id: str
    expected_version: int
    subject: str
    surface_id: str
    choice: str
    nonce: str
    issued_at: float | None = None
    auth_context: dict = None  # type: ignore[assignment]

    @classmethod
    def from_dict(cls, d: dict) -> "ExperienceResponseEnvelope":
        missing = [k for k in _RESPONSE_REQUIRED if d.get(k) in (None, "")]
        if missing:
            raise ValueError(f"experience response missing: {', '.join(missing)}")
        ev = d["expected_version"]
        if isinstance(ev, bool) or not isinstance(ev, int):
            raise ValueError("expected_version must be an integer")
        ac = d.get("auth_context")
        if ac is not None and not isinstance(ac, dict):
            raise ValueError("auth_context must be an object")
        return cls(
            response_id=d["response_id"], experience_id=d["experience_id"],
            expected_version=ev, subject=d["subject"],
            surface_id=d["surface_id"], choice=d["choice"], nonce=d["nonce"],
            issued_at=d.get("issued_at"), auth_context=ac or {},
        )
