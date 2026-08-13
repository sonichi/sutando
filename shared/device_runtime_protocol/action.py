"""The act() envelope: one device-bound execution attempt of a durable Task.

The Task envelope is NOT replaced (owner ruling): a Task owns the user goal
and may outlive many Actions; an Action binds one capability call to one
device/provider/session. `action_id` is stable across retries; `attempt`
numbers each delivery; `idempotency_key` is what prevents a redelivered
attempt from producing the external side effect twice.

`canonical_action`/`action_digest` exist NOW so S2 can bind approval tokens
to a digest without reshaping this schema later.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .capabilities import validate_capability_name
from .errors import ProtocolFault, fault

ACTION_STATUSES = ("completed", "failed")

_REQUIRED = ("action_id", "task_id", "device_id", "provider", "capability",
             "operation")


@dataclass
class ActionEnvelope:
    action_id: str
    task_id: str
    device_id: str
    provider: str
    capability: str
    operation: str
    arguments: dict = field(default_factory=dict)
    preconditions: dict = field(default_factory=dict)
    session_id: str | None = None
    attempt: int = 1
    idempotency_key: str | None = None
    deadline: float | None = None  # epoch seconds; expired actions never run
    policy: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)
    protocol_version: str = "1"

    @classmethod
    def from_dict(cls, d: dict) -> "ActionEnvelope":
        missing = [k for k in _REQUIRED if not d.get(k)]
        if missing:
            raise ValueError(f"action envelope missing: {', '.join(missing)}")
        if not validate_capability_name(d["capability"]):
            raise ValueError(f"bad capability name: {d['capability']!r}")
        attempt = d.get("attempt", 1)
        if not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        return cls(
            action_id=d["action_id"], task_id=d["task_id"],
            device_id=d["device_id"], provider=d["provider"],
            capability=d["capability"], operation=d["operation"],
            arguments=d.get("arguments") or {},
            preconditions=d.get("preconditions") or {},
            session_id=d.get("session_id"), attempt=attempt,
            idempotency_key=d.get("idempotency_key"),
            deadline=d.get("deadline"), policy=d.get("policy") or {},
            trace=d.get("trace") or {},
            protocol_version=str(d.get("protocol_version", "1")),
        )

    def to_dict(self) -> dict:
        return {
            "protocol_version": self.protocol_version,
            "action_id": self.action_id, "task_id": self.task_id,
            "device_id": self.device_id, "provider": self.provider,
            "capability": self.capability, "operation": self.operation,
            "arguments": self.arguments, "preconditions": self.preconditions,
            "session_id": self.session_id, "attempt": self.attempt,
            "idempotency_key": self.idempotency_key, "deadline": self.deadline,
            "policy": self.policy, "trace": self.trace,
        }


def canonical_action(env: ActionEnvelope) -> str:
    """Digest input: the fields that DEFINE the action's external meaning.
    attempt and trace are excluded on purpose — a retry of the same action
    must digest identically, or S2's digest-bound approvals break on retry."""
    core = {
        "protocol_version": env.protocol_version,
        "action_id": env.action_id,
        "task_id": env.task_id,
        "device_id": env.device_id,
        "provider": env.provider,
        "capability": env.capability,
        "operation": env.operation,
        "arguments": env.arguments,
        "preconditions": env.preconditions,
        "session_id": env.session_id,
        "idempotency_key": env.idempotency_key,
    }
    return json.dumps(core, sort_keys=True, separators=(",", ":"))


def action_digest(env: ActionEnvelope) -> str:
    return "sha256:" + hashlib.sha256(canonical_action(env).encode()).hexdigest()


@dataclass
class ActionResult:
    action_id: str
    attempt: int
    status: str  # completed | failed
    result: dict = field(default_factory=dict)
    fault: ProtocolFault | None = None
    observation_ref: str | None = None
    started_at: float | None = None
    completed_at: float | None = None

    def __post_init__(self) -> None:
        if self.status not in ACTION_STATUSES:
            raise ValueError(f"bad action status: {self.status!r}")
        if self.status == "failed" and self.fault is None:
            self.fault = fault("EXECUTION_FAILED", "failed without detail")

    def to_dict(self) -> dict:
        d = {
            "action_id": self.action_id, "attempt": self.attempt,
            "status": self.status, "result": self.result,
            "observation_ref": self.observation_ref,
            "started_at": self.started_at, "completed_at": self.completed_at,
        }
        if self.fault:
            d["fault"] = self.fault.to_dict()
        return d
