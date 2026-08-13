"""S1 dispatch for act() and present() — protocol semantics, no real providers.

This module is the Runtime Host's enforcement point: capability grammar, the
supported/available/granted three-set gate, deadlines, preconditions,
idempotent redelivery, experience lifecycle with optimistic versioning, and
auditable suppression. Transport-independent by construction: both the local
runtime-api transport and a relay-carried payload call the same two entry
points with plain dicts.

S1 ships ONE fake provider and TWO capability-different fake renderers; they
exist to pin the contract so BrowserProvider (S3) cannot accidentally become
the protocol's definition.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_shared = str(Path(__file__).resolve().parent.parent.parent / "shared")
if _shared not in sys.path:
    sys.path.insert(0, _shared)

from device_runtime_protocol import (  # noqa: E402
    ActionEnvelope,
    ActionResult,
    PresentEnvelope,
    PresentResult,
    fault,
    resolve_capability_state,
    validate_capability_name,
)


# ── fake provider (the S1 contract fixture) ─────────────────────────────────

class FakeCounterProvider:
    """One capability: fake.counter.increment. Preconditions carry the
    expected current value — a stale expectation is the generic
    PRECONDITION_FAILED, not an execution error. The idempotency ledger
    returns the ORIGINAL result on redelivery without re-executing."""

    name = "fake"

    def __init__(self) -> None:
        self.counter = 0
        self.executions = 0                      # proves dedup: real runs only
        self._ledger: dict[str, ActionResult] = {}
        self.unavailable_reason: str | None = None

    def supported_capabilities(self) -> set[str]:
        return {"fake.counter.increment"}

    def availability(self) -> dict[str, str]:
        if self.unavailable_reason:
            return {"fake.counter.increment": self.unavailable_reason}
        return {}

    def execute(self, env: ActionEnvelope, emit_progress) -> ActionResult:
        key = env.idempotency_key or f"{env.action_id}"
        if key in self._ledger:
            return self._ledger[key]
        expected = env.preconditions.get("counter")
        if expected is not None and expected != self.counter:
            return ActionResult(
                action_id=env.action_id, attempt=env.attempt, status="failed",
                fault=fault("PRECONDITION_FAILED",
                            f"counter is {self.counter}, precondition expected {expected}"),
            )
        emit_progress({"action_id": env.action_id, "stage": "incrementing"})
        started = time.time()
        self.counter += int(env.arguments.get("by", 1))
        self.executions += 1
        result = ActionResult(
            action_id=env.action_id, attempt=env.attempt, status="completed",
            result={"counter": self.counter},
            started_at=started, completed_at=time.time(),
        )
        self._ledger[key] = result
        return result


# ── act() dispatch ──────────────────────────────────────────────────────────

def dispatch_action(raw: dict, *, providers: dict, granted: set[str],
                    emit_progress=lambda e: None, now=None) -> dict:
    """Plain-dict in, plain-dict out — the transport-independence seam."""
    try:
        env = ActionEnvelope.from_dict(raw)
    except ValueError as e:
        return ActionResult(
            action_id=str(raw.get("action_id") or "?"),
            attempt=int(raw.get("attempt") or 1), status="failed",
            fault=fault("INVALID_ARGUMENT", str(e)),
        ).to_dict()

    def failed(f) -> dict:
        return ActionResult(action_id=env.action_id, attempt=env.attempt,
                            status="failed", fault=f).to_dict()

    provider = providers.get(env.provider)
    if provider is None:
        return failed(fault("CAPABILITY_UNSUPPORTED",
                            f"no provider {env.provider!r} on this runtime"))
    state = resolve_capability_state(
        env.capability,
        supported=provider.supported_capabilities(),
        availability=provider.availability(),
        granted=granted,
    )
    if not state.supported:
        return failed(fault("CAPABILITY_UNSUPPORTED",
                            f"{env.capability} is not implemented by {env.provider}"))
    if not state.available:
        return failed(fault("CAPABILITY_UNAVAILABLE",
                            f"{env.capability} unavailable", reason=state.reason))
    if not state.granted:
        return failed(fault("PERMISSION_DENIED",
                            f"{env.capability} not granted to this subject"))
    if env.deadline is not None and (now or time.time()) > env.deadline:
        return failed(fault("DEADLINE_EXCEEDED",
                            "action expired before execution"))
    return provider.execute(env, emit_progress).to_dict()


# ── fake renderers (two capability-different surfaces) ──────────────────────

class FakeRenderer:
    """Declares surface affordances; records what it delivered. The wrist-like
    surface caps actions at 2 and drops tables; the chat-like one is rich."""

    def __init__(self, surface_id: str, capabilities: set[str], max_actions: int):
        self.surface_id = surface_id
        self.capabilities = capabilities
        self.max_actions = max_actions
        self.delivered: list[dict] = []

    def render(self, env: PresentEnvelope, version: int) -> str:
        content = dict(env.content)
        if "table" in content and "table" not in self.capabilities:
            content.pop("table")                 # degrade, never fail render
        actions = (env.interaction.get("actions") or [])[: self.max_actions]
        ref = f"{self.surface_id}:{env.experience_id}:v{version}"
        self.delivered.append({"ref": ref, "content": content,
                               "actions": actions, "operation": env.operation})
        return ref


# ── experience store (S1: in-memory; durable store is deliberately out) ─────

class ExperienceRegistry:
    def __init__(self) -> None:
        self._exp: dict[str, dict] = {}

    def get(self, experience_id: str) -> dict | None:
        return self._exp.get(experience_id)

    def create(self, env: PresentEnvelope) -> dict:
        rec = {"experience_id": env.experience_id, "task_id": env.task_id,
               "intent": env.intent, "version": 1, "state": "active",
               "content": env.content, "audience": env.audience,
               "responses": []}
        self._exp[env.experience_id] = rec
        return rec

    def respond(self, experience_id: str, subject: str, choice: str) -> dict:
        """Authenticated input recorded ONCE. This never authorizes anything —
        the Policy Engine (S2) reads it; renderers/bridges hold no authority."""
        rec = self._exp.get(experience_id)
        if rec is None:
            raise KeyError(experience_id)
        if rec["responses"]:
            raise ValueError("already responded")
        if rec["state"] != "active":
            raise ValueError(f"experience is {rec['state']}")
        rec["responses"].append({"subject": subject, "choice": choice,
                                 "at": time.time()})
        return rec


def dispatch_present(raw: dict, *, registry: ExperienceRegistry,
                     renderers: list) -> dict:
    try:
        env = PresentEnvelope.from_dict(raw)
    except ValueError as e:
        return PresentResult(
            presentation_id=str(raw.get("presentation_id") or "?"),
            experience_id=str(raw.get("experience_id") or "?"),
            status="failed", fault=fault("INVALID_ARGUMENT", str(e)),
        ).to_dict()

    def failed(f) -> dict:
        return PresentResult(presentation_id=env.presentation_id,
                             experience_id=env.experience_id,
                             status="failed", fault=f).to_dict()

    disposition = env.delivery_policy.get("disposition", "deliver")
    rec = registry.get(env.experience_id)

    if env.operation == "create":
        if rec is not None:
            return failed(fault("CONFLICT",
                                f"experience {env.experience_id} already exists"))
        # An explicit decision to stay quiet completes WITHOUT any renderer
        # delivery — auditable, terminal, never mistakable for a lost message.
        if disposition == "suppress":
            return PresentResult(
                presentation_id=env.presentation_id,
                experience_id=env.experience_id, status="completed",
                disposition="suppressed",
                reason=env.delivery_policy.get("reason", "policy"),
            ).to_dict()
        rec = registry.create(env)
        refs = [r.render(env, rec["version"]) for r in renderers]
        return PresentResult(
            presentation_id=env.presentation_id,
            experience_id=env.experience_id, status="completed",
            disposition="delivered", experience_version=rec["version"],
            delivery_refs=refs,
        ).to_dict()

    if rec is None:
        return failed(fault("PRECONDITION_FAILED",
                            f"experience {env.experience_id} does not exist"))

    if env.operation == "update":
        if rec["state"] != "active":
            return failed(fault("CONFLICT", f"experience is {rec['state']}"))
        if env.expected_version != rec["version"]:
            return failed(fault("CONFLICT",
                                f"expected_version {env.expected_version} "
                                f"!= current {rec['version']}"))
        rec["version"] += 1
        rec["content"] = env.content or rec["content"]
        refs = [] if disposition == "update_silently" else \
            [r.render(env, rec["version"]) for r in renderers]
        return PresentResult(
            presentation_id=env.presentation_id,
            experience_id=env.experience_id, status="completed",
            disposition="updated", experience_version=rec["version"],
            delivery_refs=refs,
        ).to_dict()

    # resolve | dismiss
    terminal = "resolved" if env.operation == "resolve" else "dismissed"
    rec["state"] = terminal
    return PresentResult(
        presentation_id=env.presentation_id, experience_id=env.experience_id,
        status="completed", disposition=terminal,
        experience_version=rec["version"],
    ).to_dict()
