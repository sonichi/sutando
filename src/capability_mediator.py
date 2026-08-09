#!/usr/bin/env python3
"""Mediated capability layer — the mediator (execution + grants + escalation + audit).

Second half of docs/design-mediated-capability-layer.md (RFC #2632); the decision
core is src/capability_policy.py. A consumer never holds a raw key, tool handle,
or merge button — it calls ``mediate(verb, args, handle, ...)`` and the mediator
resolves, authorizes, executes, and audits against the single policy:

    request -> derive principal (from a trusted, mediator-owned handle)
            -> decide(...) [capability_policy]
            -> allow:       execute -> verify outcome -> audit
               delegate:    return a sandboxed-delegate directive       -> audit
               needs-auth:  covering grant? consume+execute+verify+audit
                            else escalate (write-then-assert) + audit(pending)
               deny/prohibited: refuse (rule cited)                     -> audit

Guarantees the RFC requires and this module enforces mechanically:
- **Trust root:** a caller never submits a principal. The principal is derived
  from an opaque handle the mediator minted against a task envelope; unknown /
  expired / closed handles are rejected.
- **Grants are the ONLY satisfier of needs-authorization.** A fresh single-use
  grant (nonce consumed before execution) or a standing scope grant — never a
  claim embedded in observed content (a string is not a grant).
- **Verified outcome:** a mutation is ``succeeded`` only when an independent
  postcondition verifier confirms it; a callee's truthy return is never enough
  (``unknown``/``failed`` otherwise).
- **Escalation delivery (write-then-assert):** an escalation is not "recorded"
  until it is read back and confirmed to COUNT (above the pending-questions
  ``# Resolved`` divider); a write whose read-back fails is a failed escalation,
  surfaced, not assumed delivered.

Dependency-light + injectable (clock, audit path, pending-questions path,
executor, verifier) so it is hermetically testable AND live-demonstrable.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from typing import Callable, NamedTuple, Optional

import capability_policy as cp


# ── Trusted context handles (RFC "Trust root") ───────────────────────────────
class _Context(NamedTuple):
    principal: cp.Principal
    task_id: str            # immutable originating task/request identity (RFC)
    expires_at: float
    closed: bool


class ContextRegistry:
    """Mints opaque handles bound to a derived principal AND the originating
    task/request identity. A handle is process-local (cross-process handles are
    rejected by construction — they never exist in this registry) and
    single-registry; unknown/expired/closed -> None."""

    def __init__(self, now: Callable[[], float] = time.time):
        self._now = now
        self._by_handle: dict = {}

    def mint(self, envelope: dict, ttl_seconds: float = 900.0) -> str:
        """Mint a handle from an authenticated task ENVELOPE. The principal AND
        the originating task id are derived here; a caller can never inject a
        tier, and the task id is retained so a fresh grant can bind to it."""
        env = envelope or {}
        principal = cp.Principal(
            tier=cp.normalize_tier(env.get("access_tier")),
            source=str(env.get("source", "")),
            user_id=str(env.get("user_id", "")),
        )
        task_id = str(env.get("id") or env.get("task_id") or "")
        handle = "cap-ctx-" + secrets.token_hex(16)
        self._by_handle[handle] = _Context(principal, task_id, self._now() + ttl_seconds, False)
        return handle

    def _live_context(self, handle: str) -> Optional[_Context]:
        ctx = self._by_handle.get(handle)
        if ctx is None or ctx.closed or self._now() >= ctx.expires_at:
            return None
        return ctx

    def derive_principal(self, handle: str) -> Optional[cp.Principal]:
        ctx = self._live_context(handle)
        return ctx.principal if ctx else None

    def derive_task_id(self, handle: str) -> Optional[str]:
        ctx = self._live_context(handle)
        return ctx.task_id if ctx else None

    def close(self, handle: str) -> None:
        ctx = self._by_handle.get(handle)
        if ctx is not None:
            self._by_handle[handle] = ctx._replace(closed=True)


# ── Authorization grants (RFC "Trust root and authorization grants") ─────────
class Grant(NamedTuple):
    grant_id: str
    verb: str
    tier: str
    user_id: str = ""          # the authenticated principal the approval was FOR
    source: str = ""           # and the source it arrived on (RFC trust-root)
    task_id: str = ""          # a FRESH grant also binds the originating task/request
    args_digest: str = ""      # fresh single-use grant binds an exact digest
    scope_pattern: str = ""    # standing grant binds a scope pattern instead
    expires_at: float = 0.0
    single_use: bool = True


def digest_args(args) -> str:
    """Stable digest of normalized args — the fresh-grant binding."""
    blob = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class GrantStore:
    """Owner-minted, enumerable, revocable. A grant is BOUND to the principal the
    approval was for (identity + source), so it is not a bearer token another
    same-tier principal can replay. A fresh grant is consumed before execution;
    a standing grant is not. Expiry is enforced on read."""

    def __init__(self, now: Callable[[], float] = time.time):
        self._now = now
        self._grants: dict = {}

    def mint_fresh(self, verb: str, principal: cp.Principal, args, task_id: str = "",
                   ttl_seconds: float = 300.0) -> Grant:
        # A fresh grant without a task_id is unbound: consume_covering() never
        # honors it (fail-closed), so it can't become a cross-task wildcard.
        g = Grant(grant_id="grant-" + secrets.token_hex(12), verb=verb,
                  tier=principal.tier, user_id=principal.user_id, source=principal.source,
                  task_id=str(task_id or ""), args_digest=digest_args(args),
                  expires_at=self._now() + ttl_seconds, single_use=True)
        self._grants[g.grant_id] = g
        return g

    def mint_standing(self, verb: str, principal: cp.Principal, scope_pattern: str,
                      ttl_seconds: float = 30 * 86400.0) -> Grant:
        g = Grant(grant_id="grant-" + secrets.token_hex(12), verb=verb,
                  tier=principal.tier, user_id=principal.user_id, source=principal.source,
                  scope_pattern=scope_pattern, expires_at=self._now() + ttl_seconds,
                  single_use=False)
        self._grants[g.grant_id] = g
        return g

    def revoke(self, grant_id: str) -> None:
        self._grants.pop(grant_id, None)

    def _live(self):
        now = self._now()
        return [g for g in self._grants.values() if g.expires_at > now]

    def consume_covering(self, req: cp.CapabilityRequest, principal: cp.Principal,
                         task_id: str = "") -> Optional[Grant]:
        """Find a LIVE covering grant BOUND to this principal and, for a FRESH
        grant, to the originating task — and if single-use, atomically consume it
        BEFORE the caller executes. A grant covers only when its verb, tier, AND
        user_id (and source, if pinned) match the principal; a FRESH grant must
        ALSO match the current task_id (fail-closed if the grant carries a task_id
        the request doesn't match) — so a grant approved for task-A cannot be
        replayed by task-B. An approval record is neither a bearer token nor
        reusable across requests. Standing grants stay scope-based (no task bind)
        and are returned without consumption (RFC)."""
        if not principal.user_id:
            return None
        for g in self._live():
            if g.verb != req.verb:
                continue
            if not g.tier or g.tier != principal.tier:
                continue
            if not g.user_id or g.user_id != principal.user_id:
                continue
            if g.source and g.source != principal.source:
                continue
            if g.single_use and g.args_digest and g.args_digest == req.args_digest:
                # A fresh grant must carry a non-empty task id matching the current
                # one; an unbound grant (task_id="") never covers (fail-closed).
                if not g.task_id or not task_id or g.task_id != task_id:
                    continue
                self._grants.pop(g.grant_id, None)   # atomic single-use consume
                return g
            if (not g.single_use) and g.scope_pattern and cp._scope_matches(g.scope_pattern, req.scope):
                return g
        return None


# ── Append-only audit log (RFC "Audit" — log-before-mutate, reconcile outcome)
class AuditLog:
    """One append-only JSONL record per request: who / capability / decision /
    VERIFIED outcome. Never raises on write (audit must not break the action)."""

    def __init__(self, path: str, now: Callable[[], float] = time.time):
        self.path = path
        self._now = now

    def record(self, principal: cp.Principal, req: cp.CapabilityRequest,
               decision: cp.Decision, outcome: str, detail: str = "") -> dict:
        row = {"ts": self._now(), "tier": principal.tier, "source": principal.source,
               "verb": req.verb, "scope": req.scope, "capability_class": decision.capability_class,
               "decision": decision.decision, "outcome": outcome, "rule": decision.rule,
               "detail": detail}
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError:
            pass  # audit is best-effort on the IO boundary; never breaks the action
        return row


# Outcome constants (verified-outcome contract).
ATTEMPTED, SUCCEEDED, FAILED, UNKNOWN = "attempted", "succeeded", "failed", "unknown"
# Non-execution outcomes.
DENIED, ESCALATED, DELEGATED, PROHIBITED_OUT = "denied", "escalated", "delegated", "prohibited"


# ── Escalation: write-then-assert to pending-questions (RFC "Escalation delivery")
_RESOLVED_DIVIDER = "# Resolved"


def escalate_pending(pq_path: str, question: str,
                     reader: Optional[Callable[[], list]] = None) -> bool:
    """Insert an escalation ABOVE the ``# Resolved`` divider (so the reader
    counts it), then READ BACK and confirm it counts. Returns True only when the
    read-back finds the entry — a written-but-uncounted escalation is a FAILED
    escalation (RFC: never a silent deny). ``reader`` returns the active
    questions; when None, a lightweight in-file check is used."""
    marker = "cap-escalation:" + secrets.token_hex(6)
    # Written as a `## ` section — the only format check-pending-questions counts
    # (a `- [ ]` bullet is silently uncounted); marker early so it survives the scan.
    entry = f"## [{marker}] {question}\n\n**Status:** unanswered\n"
    try:
        existing = ""
        if os.path.exists(pq_path):
            with open(pq_path, "r", encoding="utf-8") as fh:
                existing = fh.read()
        if _RESOLVED_DIVIDER in existing:
            head, _, tail = existing.partition(_RESOLVED_DIVIDER)
            new = head.rstrip("\n") + "\n\n" + entry + "\n" + _RESOLVED_DIVIDER + tail
        else:
            new = (existing.rstrip("\n") + "\n\n" if existing else "") + entry
        os.makedirs(os.path.dirname(pq_path) or ".", exist_ok=True)
        with open(pq_path, "w", encoding="utf-8") as fh:
            fh.write(new)
    except OSError:
        return False
    # read-back assert: the entry must be visible to the counting reader.
    try:
        if reader is not None:
            return any(marker in str(q) for q in reader())
        with open(pq_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        active = content.split(_RESOLVED_DIVIDER, 1)[0]
        return marker in active   # above the divider == counts
    except OSError:
        return False


# ── The mediator ─────────────────────────────────────────────────────────────
class MediationResult(NamedTuple):
    decision: str
    outcome: str
    result: object = None      # the executor's result on a verified success
    audit: Optional[dict] = None
    detail: str = ""


# A verifier confirms the declared postcondition; a raw truthy return is NOT it.
Executor = Callable[[cp.CapabilityRequest], object]
Verifier = Callable[[cp.CapabilityRequest, object], bool]


class Mediator:
    def __init__(self, contexts: ContextRegistry, grants: GrantStore, audit: AuditLog,
                 pq_path: str = "", pq_reader: Optional[Callable[[], list]] = None,
                 prohibited_overlay=cp.DEFAULT_PROHIBITED_OVERLAY):
        self.contexts = contexts
        self.grants = grants
        self.audit = audit
        self.pq_path = pq_path
        self.pq_reader = pq_reader
        self.overlay = prohibited_overlay

    def mediate(self, verb: str, args, handle: str,
                executor: Optional[Executor] = None,
                verifier: Optional[Verifier] = None,
                scope: str = "") -> MediationResult:
        req = cp.CapabilityRequest(verb=verb, scope=scope, args_digest=digest_args(args))
        principal = self.contexts.derive_principal(handle)
        if principal is None:
            # No valid handle => no principal can be derived => fail closed.
            d = cp.Decision(cp.DENY, "unknown", "invalid/expired/closed context handle")
            row = self.audit.record(cp.Principal(tier=cp.OTHER), req, d, DENIED)
            return MediationResult(cp.DENY, DENIED, None, row, "no valid context handle")
        task_id = self.contexts.derive_task_id(handle) or ""  # bind a fresh grant to THIS task

        # Grants not applied here — GrantStore is the single place a live grant is
        # consulted AND atomically consumed (below), so consume can't drift.
        decision = cp.decide(req, principal, grants=None, prohibited_overlay=self.overlay)

        if decision.decision == cp.PROHIBITED:
            row = self.audit.record(principal, req, decision, PROHIBITED_OUT)
            return MediationResult(cp.PROHIBITED, PROHIBITED_OUT, None, row, decision.rule)

        if decision.decision == cp.DENY:
            row = self.audit.record(principal, req, decision, DENIED)
            return MediationResult(cp.DENY, DENIED, None, row, decision.rule)

        if decision.decision == cp.DELEGATE:
            row = self.audit.record(principal, req, decision, DELEGATED)
            return MediationResult(cp.DELEGATE, DELEGATED, None, row,
                                   "run under codex --sandbox read-only, no mutation")

        if decision.decision == cp.NEEDS_AUTH:
            grant = self.grants.consume_covering(req, principal, task_id)  # single-use consumed here
            if grant is not None:
                allowed = cp.Decision(cp.ALLOW, decision.capability_class,
                                      f"{decision.capability_class}/{principal.tier} "
                                      f"needs-authorization satisfied by grant {grant.grant_id}")
                return self._execute(principal, req, allowed, executor, verifier)
            delivered = escalate_pending(
                self.pq_path,
                f"Authorize {verb} (scope={scope or 'n/a'}) for {principal.tier}?",
                reader=self.pq_reader) if self.pq_path else False
            row = self.audit.record(principal, req, decision, ESCALATED,
                                    f"escalation delivered={delivered}")
            return MediationResult(cp.NEEDS_AUTH, ESCALATED, None, row,
                                   f"escalated; delivered={delivered}")

        # decision == ALLOW (a base-allow matrix cell)
        return self._execute(principal, req, decision, executor, verifier)

    def _execute(self, principal, req, decision, executor, verifier) -> MediationResult:
        if executor is None:
            # allowed but nothing to run (e.g., a pure read the caller handles)
            row = self.audit.record(principal, req, decision, SUCCEEDED, "no executor (allow only)")
            return MediationResult(cp.ALLOW, SUCCEEDED, None, row, decision.rule)
        # log-before-mutate
        self.audit.record(principal, req, decision, ATTEMPTED)
        try:
            raw = executor(req)
        except Exception as e:  # a failed mutation is FAILED, never success
            row = self.audit.record(principal, req, decision, FAILED, f"executor raised: {e!r}")
            return MediationResult(cp.ALLOW, FAILED, None, row, "executor raised")
        # verified outcome: a truthy return is NOT sufficient — an independent
        # verifier must confirm the postcondition. No verifier => unknown.
        if verifier is None:
            row = self.audit.record(principal, req, decision, UNKNOWN,
                                    "no postcondition verifier declared")
            return MediationResult(cp.ALLOW, UNKNOWN, raw, row, "no verifier")
        try:
            ok = bool(verifier(req, raw))
        except Exception as e:
            ok = False
            row = self.audit.record(principal, req, decision, UNKNOWN, f"verifier raised: {e!r}")
            return MediationResult(cp.ALLOW, UNKNOWN, raw, row, "verifier raised")
        outcome = SUCCEEDED if ok else FAILED
        row = self.audit.record(principal, req, decision, outcome)
        return MediationResult(cp.ALLOW, outcome, raw if ok else None, row, decision.rule)
