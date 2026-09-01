"""Delivery Core contract: the channel-neutral seam between local ownership
(ClaimBackend), the external side effect (DeliveryProvider), and the drain
loop (DeliveryCore in core.py).

Identity model (three types, never conflated):
- item_id: stable logical message identity, assigned at publish.
- ClaimToken: one local-ownership incarnation; OPAQUE above the backend and
  NEVER exported as delivery identity — claim material (worker/pid/birth)
  changes across incarnations, and a key derived from it turns provider
  dedup into a fresh send.
- delivery idempotency key: stable per logical external side effect, minted
  by DeliveryCore from item_id (+ deliberate re-send epoch), passed to the
  provider, and REUSED on every retry and across re-claim/restart.

Guarantee wording (normative): effectively-once within the provider's
idempotency and receipt-retention contract — never unqualified
"exactly-once".

Retirement authority (normative, from the #3018 cross-consumer defect):
the consumer that DISPATCHED a work item is the only one that may retire
its result (archive / ack / skip-mark). A consumer sweeping a shared
results namespace must filter to its own dispatches before acting —
retiring another consumer's bookkeeping strands the replies behind it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol, runtime_checkable


class DeliveryOutcome(str, Enum):
    """Describes an ATTEMPTED external delivery, and nothing else. A drain
    that never called the provider has no DeliveryOutcome — see DrainResult
    — or telemetry and recovery policy read local bookkeeping as external
    ambiguity."""
    CONFIRMED = "confirmed"
    NOT_DELIVERED = "not_delivered"
    OUTCOME_UNKNOWN = "outcome_unknown"


class DrainStatus(str, Enum):
    """Why a drain attempt ended, locally. Neither non-ATTEMPTED status made
    a provider call, so nothing external is ambiguous in either.

    NOT_CLAIMED and TERMINAL are split because they differ in whether a later
    pass can ever succeed: NOT_CLAIMED is contention (another worker owns it;
    a reclaim recovers it), TERMINAL is the item's own final state. Collapsing
    them makes a drainer retry a decided item forever.
    """
    NOT_CLAIMED = "not_claimed"
    TERMINAL = "terminal"
    ATTEMPTED = "attempted"


@dataclass(frozen=True)
class DrainResult:
    status: DrainStatus
    outcome: Optional[DeliveryOutcome] = None   # set iff status is ATTEMPTED

    def __post_init__(self):
        attempted = self.status is DrainStatus.ATTEMPTED
        if attempted != (self.outcome is not None):
            raise ValueError(
                "an outcome exists exactly when a delivery was attempted")


class ProviderIndeterminate(Exception):
    """The call MAY have crossed the external side-effect boundary (timeout
    after send, connection reset mid-request, ambiguous 5xx). This is the
    ONLY exception class that maps to OUTCOME_UNKNOWN."""


class ProviderRefused(Exception):
    """The provider definitely did not perform the side effect (validation
    rejection, refusal before dispatch). Maps to NOT_DELIVERED."""


@dataclass(frozen=True)
class DeliveryReceipt:
    """Outcome of one deliver/reconcile call. `provider_ref` is the
    provider's own reference for the side effect; confirmed-by-provider is
    never inferred from accepted-by-transport, so CONFIRMED without a
    provider_ref is legal only where the provider's contract says accept
    IS confirmation."""
    outcome: DeliveryOutcome
    provider_ref: Optional[str] = None
    detail: str = ""
    # Where the side effect landed, in the provider's own address space.
    # Only the provider knows this; the core must not infer it.
    destination: Optional[str] = None


@dataclass(frozen=True)
class DeliveryAttempt:
    """The full attempt an UNKNOWN outcome refers to. reconcile takes this,
    not bare ids: a provider whose only receipt store is the send itself
    (idempotent) reconciles BY safe re-send, which needs the payload."""
    item_id: str
    payload: bytes
    idempotency_key: str


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declared, suite-read; decides post-UNKNOWN behavior. Channel code
    never does."""
    reconcile_capable: bool = False   # receipt queryable after UNKNOWN
    idempotent_send: bool = False     # key-deduped; safe-resend on UNKNOWN


@dataclass(frozen=True)
class BackendCapabilities:
    """Declared, suite-read (never hasattr-sniffed). What is optional is
    the OPERATION, not the liveness guarantee it serves: "a dead owner's
    claim is eventually recoverable" holds for every backend — force
    release is one mechanism (A), a requeue-layer path is another (B)."""
    supports_force_release: bool = False


@dataclass(frozen=True)
class ClaimToken:
    """ONE ownership incarnation, not a drainer identity. `incarnation` is
    backend-private material that a later incarnation of the same worker
    cannot reproduce (pid + process birth + claim stamp), so a token that
    outlived its claim is rejected instead of matching by worker name.
    Opaque above the backend; item_id rides along for routing only."""
    item_id: str
    worker: str
    incarnation: str


@dataclass
class CleanupReport:
    pruned: int = 0
    detail: str = ""


@dataclass
class DrainReport:
    claimed: int = 0
    confirmed: int = 0
    retried: int = 0
    parked: int = 0
    errors: list = field(default_factory=list)


@dataclass
class RecoverReport:
    recovered: list = field(default_factory=list)   # item_ids re-claimable
    quarantined: list = field(default_factory=list)
    retired: list = field(default_factory=list)     # dead claims on TERMINAL items


@runtime_checkable
class ClaimBackend(Protocol):
    """Local ownership of published outbound items. Invariants the contract
    suite enforces on EVERY implementation (see the seam doc for the full
    instrument mapping):
      1. at most one local owner per item at any time — including under the
         publish-during-inflight schedule (CE-1);
      2. a stale incarnation can never destroy a successful claimant;
      3. crash at any syscall boundary leaves the item in exactly one
         deliverable state or with a terminal record;
      4. protocol metadata is bounded (cleanup);
      5. a dead owner's claim is eventually recoverable (mechanism per
         capabilities);
      6. lost races are protocol outcomes; config errors raise loudly."""

    @property
    def capabilities(self) -> BackendCapabilities: ...

    def publish(self, item_id: str, payload: bytes) -> bool:
        """True = newly published; False = this id is already live."""
        ...

    def claim(self, item_id: str, worker: str) -> Optional[ClaimToken]:
        """Acquire exclusive local ownership, or None when it was not taken.

        None does NOT mean "another worker owns it" — it also covers an item
        that is already terminal, absent, or whose incarnation vanished. Ask
        `is_terminal` to tell a decided item from a contended one.
        """
        ...

    def is_terminal(self, item_id: str) -> bool:
        """True when this item has reached its own final state (delivered or
        parked) and no drain will ever claim it again.

        Only meaningful after a failed `claim`, and read separately because a
        terminal item and a contended one are indistinguishable from `claim`
        alone. Terminal is absorbing except via the administrative requeue
        path, which resets the attempt budget — so a `True` that goes stale
        that way costs one extra pass, never a stuck item.
        """
        ...

    # False = complete() accepts provider/destination and DROPS them.
    # Check this; a signature does not imply durable storage.
    persists_receipt_metadata: bool = False

    def complete(self, token: ClaimToken, outcome: DeliveryOutcome,
                 park_at_attempts: Optional[int] = None,
                 provider: Optional[str] = None,
                 destination: Optional[str] = None) -> bool:
        """Validate the exact incarnation, apply the outcome transition, and
        retire the claim — ALL inside one backend critical section, in that
        order. A stale token must change nothing: validating after mutating
        lets a dead incarnation park or advance its successor's item, which
        is the concurrency defect this seam exists to stop propagating.
        `park_at_attempts` makes the retry CEILING part of the same atomic
        step: recording the attempt and parking at the ceiling must not be
        two transactions, or a successor can claim and confirm between them
        and the stale caller's park overwrites its DELIVERED state.
        True = this incarnation owned the claim and it is now retired."""
        ...

    def attempts(self, item_id: str) -> int:
        """Delivery attempts recorded for this item (retry accounting)."""
        ...

    def park(self, item_id: str, reason: str) -> None:
        """Remove the item from the drainable set pending operator action."""
        ...

    def recover(self) -> RecoverReport:
        """Return DEAD owners' items to claimable. ALIVE/UNKNOWN owners are
        never touched; a dead token whose key has another live holder is
        quarantined, never re-armed (CE-2/CE-1 class)."""
        ...

    def cleanup(self) -> CleanupReport:
        """Bound on-disk protocol state."""
        ...

    def force_release(self, item_id: str) -> bool:
        """ADMINISTRATIVE DESTRUCTION, not a release (contract: #3008).
        Only for backends declaring supports_force_release; others raise
        NotImplementedError — never a silent no-op."""
        ...


@runtime_checkable
class DeliveryProvider(Protocol):
    """The external side effect. Adapters keep provider-specific mechanics
    (AG2 Space lease/ACK/finalize, Discord API + rate limits); the core
    reads only capabilities and receipts. Adapter contract tests must
    export a finalize-observable where the provider has a server-side
    finalize, so provider-confirmed and server-finalized stay independently
    observable (invariant 9)."""

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def deliver(self, item_id: str, payload: bytes,
                idempotency_key: str) -> DeliveryReceipt: ...

    def reconcile(self, attempt: DeliveryAttempt) -> Optional[DeliveryReceipt]:
        """Resolve an OUTCOME_UNKNOWN for `attempt`; None where the provider
        cannot answer (capability-gated). The attempt carries the payload so
        a keyed-dedup provider may reconcile by safe re-send."""
        ...
