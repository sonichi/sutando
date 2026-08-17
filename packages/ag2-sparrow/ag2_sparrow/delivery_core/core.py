"""DeliveryCore: the drain loop. The ONLY component that reads
DeliveryOutcome semantics and ProviderCapabilities — channel code never
decides retry/UNKNOWN rules (acceptance criterion 2).

Retry policy (normative):
- only a confirmed NOT_DELIVERED auto-retries;
- OUTCOME_UNKNOWN parks unless capabilities license reconcile (resolve,
  then act on the resolved outcome) or idempotent-send (safe re-send);
- ambiguous is never auto-relabeled NOT_DELIVERED.

Delivered evidence is written only AFTER the send returns (evidence is
risk control, not proof — invariant 8)."""
from __future__ import annotations

from dataclasses import dataclass

from .contract import (BackendCapabilities, ClaimBackend, DeliveryOutcome,
                       DeliveryProvider, DrainReport, RecoverReport)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3


def idempotency_key(item_id: str, resend_epoch: int = 0) -> str:
    """Stable per logical side effect; NEVER derived from claim material.
    resend_epoch changes only on a DELIBERATE operator re-send."""
    return f"{item_id}#{resend_epoch}"


class DeliveryCore:
    def __init__(self, backend: ClaimBackend, provider: DeliveryProvider,
                 policy: RetryPolicy | None = None,
                 worker: str = "delivery-core"):
        self.backend = backend
        self.provider = provider
        self.policy = policy or RetryPolicy()
        self.worker = worker

    def deliver_one(self, item_id: str, payload: bytes) -> DeliveryOutcome:
        """Claim -> deliver -> classify -> complete/park. Returns the final
        outcome recorded for this pass (OUTCOME_UNKNOWN when parked)."""
        token = self.backend.claim(item_id, self.worker)
        if token is None:
            return DeliveryOutcome.OUTCOME_UNKNOWN     # lost race: not ours
        key = idempotency_key(item_id)
        try:
            receipt = self.provider.deliver(item_id, payload, key)
        except Exception:
            # transport indeterminacy: the local call and the remote effect
            # are never one transaction — UNKNOWN is irreducible locally
            receipt = None
        outcome = receipt.outcome if receipt is not None \
            else DeliveryOutcome.OUTCOME_UNKNOWN
        if outcome is DeliveryOutcome.OUTCOME_UNKNOWN:
            caps = self.provider.capabilities
            if caps.reconcile_capable:
                resolved = self.provider.reconcile(item_id, key)
                if resolved is not None:
                    outcome = resolved.outcome
            elif caps.idempotent_send:
                try:
                    retry = self.provider.deliver(item_id, payload, key)
                    outcome = retry.outcome
                except Exception:
                    outcome = DeliveryOutcome.OUTCOME_UNKNOWN
        self.backend.complete(token, outcome)
        return outcome

    def recover(self) -> RecoverReport:
        return self.backend.recover()
