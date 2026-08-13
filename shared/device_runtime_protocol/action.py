"""The act() envelope: one device-bound execution attempt of a durable Task.

The Task envelope is NOT replaced: a Task owns the user goal and may outlive
many Actions; an Action binds one capability call to one
device/provider/session. `action_id` is stable across retries; `attempt`
numbers each delivery; `idempotency_key` is what prevents a redelivered
attempt from producing the external side effect twice.

S1.1: protocol_version is ENFORCED (only "1" parses — an unknown version is
refused, never silently read with v1 semantics); the digest uses the strict
canonical profile in canonical.py, with golden vectors pinning bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .canonical import canonical_digest, canonical_json, drop_absent
from .capabilities import validate_capability_name
from .errors import ProtocolFault, fault

SUPPORTED_PROTOCOL_VERSIONS = ("1",)
ACTION_STATUSES = ("completed", "failed")

_REQUIRED = ("action_id", "task_id", "device_id", "provider", "capability",
             "operation")


def _require_version(d: dict) -> str:
    v = d.get("protocol_version")
    version = "1" if v is None else str(v)
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ValueError(f"unsupported protocol_version: {version!r} "
                         f"(this runtime speaks {SUPPORTED_PROTOCOL_VERSIONS})")
    return version


def _strict_int(value, name: str) -> int:
    # bool is an int subclass in Python; attempt=true must not parse.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


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
        version = _require_version(d)
        missing = [k for k in _REQUIRED if not d.get(k)]
        if missing:
            raise ValueError(f"action envelope missing: {', '.join(missing)}")
        if not validate_capability_name(d["capability"]):
            raise ValueError(f"bad capability name: {d['capability']!r}")
        if d["capability"].split(".", 1)[0] != d["provider"]:
            raise ValueError(
                f"capability {d['capability']!r} does not belong to "
                f"provider {d['provider']!r}")
        attempt = _strict_int(d.get("attempt", 1), "attempt")
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        for k in ("arguments", "preconditions", "policy", "trace"):
            v = d.get(k)
            if v is not None and not isinstance(v, dict):
                raise ValueError(f"{k} must be an object")
        deadline = d.get("deadline")
        if deadline is not None:
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
                raise ValueError("deadline must be a finite number")
            deadline = float(deadline)
            if deadline != deadline or deadline in (float("inf"), float("-inf")):
                raise ValueError("deadline must be a finite number")
        return cls(
            action_id=d["action_id"], task_id=d["task_id"],
            device_id=d["device_id"], provider=d["provider"],
            capability=d["capability"], operation=d["operation"],
            arguments=d.get("arguments") or {},
            preconditions=d.get("preconditions") or {},
            session_id=d.get("session_id"), attempt=attempt,
            idempotency_key=d.get("idempotency_key"),
            deadline=deadline, policy=d.get("policy") or {},
            trace=d.get("trace") or {},
            protocol_version=version,
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
    """Canonical bytes for the fields that DEFINE the action's external
    meaning. attempt and trace are excluded on purpose — a retry of the same
    action must digest identically, or digest-bound approvals break on retry.
    Absent optionals are OMITTED (profile rule: no nulls)."""
    core = drop_absent({
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
    })
    return canonical_json(core)


def action_digest(env: ActionEnvelope) -> str:
    core = drop_absent({
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
    })
    return canonical_digest(core)


def effects_digest(effects: list[str]) -> str:
    """Digest of the EXACT effects list shown to a human. The approval binding
    carries both this and the action digest so 'what was displayed' is
    provably 'what was approved' (S1.1 ruling #3)."""
    if not effects or not all(isinstance(e, str) and e for e in effects):
        raise ValueError("effects must be a non-empty list of non-empty strings")
    return canonical_digest({"effects": effects})


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
