"""DeliveryCore: the drain loop. The ONLY component that reads
DeliveryOutcome semantics and ProviderCapabilities — channel code never
decides retry/UNKNOWN rules (acceptance criterion 2).

Retry policy (normative):
- only a confirmed NOT_DELIVERED auto-retries;
- OUTCOME_UNKNOWN parks unless capabilities license reconcile (resolve,
  then act on the resolved outcome) or idempotent-send (safe re-send;
  still-ambiguous after the re-send completes as a retryable attempt —
  the license makes every later re-send exactly as safe as this one);
- absent such a license, ambiguous is never auto-relabeled NOT_DELIVERED.

Delivered evidence is written only AFTER the send returns (evidence is
risk control, not proof — invariant 8)."""
from __future__ import annotations

from dataclasses import dataclass

from .contract import (ClaimBackend, DeliveryAttempt, DeliveryOutcome, DeliveryProvider,
                       DrainResult, DrainStatus, ProviderIndeterminate,
                       ProviderRefused, RecoverReport)


@dataclass(frozen=True)
class RetryPolicy:
    """The park ceiling is mandatory: an adapter may raise it, never remove it.
    An unbounded retry is a duplicate generator, not a resilience setting."""
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError(
                f"max_attempts must be a positive int, got {self.max_attempts!r}")


def idempotency_key(item_id: str, resend_epoch: int = 0) -> str:
    """Stable per logical side effect; NEVER derived from claim material.
    resend_epoch changes only on a DELIBERATE operator re-send."""
    return f"{item_id}#{resend_epoch}"


def _resend_epoch(backend, item_id: str) -> int:
    """0 for a backend that does not track re-sends: the key stays `id#0`,
    which is exactly the pre-existing behaviour."""
    fn = getattr(backend, "resend_epoch", None)
    return int(fn(item_id)) if callable(fn) else 0


class DeliveryCore:
    def __init__(self, backend: ClaimBackend, provider: DeliveryProvider,
                 policy: RetryPolicy | None = None,
                 worker: str = "delivery-core"):
        self.backend = backend
        self.provider = provider
        self.policy = policy or RetryPolicy()
        self.worker = worker

    def _attempt(self, item_id: str, payload: bytes,
                 key: str) -> DeliveryOutcome:
        """One provider call, classified by the typed failure taxonomy.
        Only a boundary-crossing failure is UNKNOWN; a refusal is
        NOT_DELIVERED; anything else (programming, config, capability
        violation) propagates rather than masquerading as ambiguity."""
        try:
            receipt = self.provider.deliver(item_id, payload, key)
            return receipt.outcome, getattr(receipt, "destination", None)
        except ProviderIndeterminate:
            return DeliveryOutcome.OUTCOME_UNKNOWN, None
        except ProviderRefused:
            return DeliveryOutcome.NOT_DELIVERED, None

    def _reconcile(self, item_id: str, payload: bytes, key: str):
        """Resolve a prior ambiguity, or None when reconciliation resolved
        NOTHING — the caller keeps the outcome it already had.

        A raise here describes the RECONCILE call, not the original send.
        ProviderRefused proves only that this second call never dispatched;
        the first may already have crossed the side-effect boundary. Only a
        reconciliation RECEIPT is a statement about the original attempt, so
        only a receipt may replace OUTCOME_UNKNOWN (sparrow-v1-contract:
        "Ambiguous is never auto-relabeled NOT_DELIVERED").
        """
        try:
            resolved = self.provider.reconcile(
                DeliveryAttempt(item_id, payload, key))
        except (ProviderIndeterminate, ProviderRefused):
            return None
        if resolved is None:
            return None
        return resolved.outcome, getattr(resolved, "destination", None)

    def deliver_one(self, item_id: str, payload: bytes) -> DrainResult:
        """Claim -> deliver -> classify -> complete, with retry accounting."""
        token = self.backend.claim(item_id, self.worker)
        if token is None:
            # No provider call either way, so nothing external is ambiguous.
            # Terminal is unclaimable forever; contention is not.
            if self.backend.is_terminal(item_id):
                return DrainResult(status=DrainStatus.TERMINAL)
            return DrainResult(status=DrainStatus.NOT_CLAIMED)
        # A requeued item must present a NEW logical side effect, or the
        # provider dedupes the re-send against the attempt that parked it.
        key = idempotency_key(item_id, _resend_epoch(self.backend, item_id))
        outcome, destination = self._attempt(item_id, payload, key)
        if outcome is DeliveryOutcome.OUTCOME_UNKNOWN:
            caps = self.provider.capabilities
            if caps.reconcile_capable:
                resolved = self._reconcile(item_id, payload, key)
                if resolved is not None:
                    # The reconciliation receipt is the statement about THIS
                    # item; its destination replaces the ambiguous attempt's.
                    outcome, destination = resolved
            elif caps.idempotent_send:
                outcome, destination = self._attempt(item_id, payload, key)
                if outcome is DeliveryOutcome.OUTCOME_UNKNOWN:
                    # Retryable by license: parking would strand an item a
                    # later safe re-send could still deliver.
                    outcome = DeliveryOutcome.NOT_DELIVERED
        # The ceiling rides WITH the completion: parking after the claim
        # is released lets a successor confirm in the gap.
        self.backend.complete(token, outcome,
                              park_at_attempts=self.policy.max_attempts,
                              provider=type(self.provider).__name__,
                              destination=destination)
        return DrainResult(status=DrainStatus.ATTEMPTED, outcome=outcome)

    def recover(self) -> RecoverReport:
        return self.backend.recover()
