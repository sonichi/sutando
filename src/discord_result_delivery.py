"""Discord result-delivery state, bound to the shared outbox (#3279 action 2).

The outbox owns claim, retry, idempotency, and the three-state outcome; the
bridge keeps channel.send mechanics only. The legacy sentinel directory
(state/discord-delivered/) is honored READ-ONLY for the migration window —
new state is written exclusively here, so there is one coordinator, not two.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ag2_sparrow.delivery_core import DesignAClaimBackend
from ag2_sparrow.delivery_core.contract import ClaimToken, DeliveryOutcome

import outbox
import result_audit

WORKER = "discord-results"
PARK_AT_ATTEMPTS = 5

_BACKENDS: dict = {}


def result_backend(results_dir: Path) -> DesignAClaimBackend:
    """Per-root singleton; the root lives INSIDE results/ so any harness that
    redirects RESULTS_DIR is hermetic for free (gateway precedent)."""
    root = Path(results_dir) / ".outbox-discord-results"
    b = _BACKENDS.get(root)
    if b is None:
        b = _BACKENDS[root] = DesignAClaimBackend(root)
    return b


def is_delivered(results_dir: Path, task_id: str,
                 legacy_sentinel_dir: Optional[Path] = None) -> bool:
    """Outbox DELIVERED is authoritative; a legacy sentinel file is honored
    read-only so a restart across the migration cannot double-send."""
    root = result_backend(results_dir).root
    if outbox.item_status(root, task_id) == "DELIVERED":
        return True
    if legacy_sentinel_dir is not None:
        # pathlib < 3.12 re-raises EACCES from exists() (only ENOENT-class
        # errnos are swallowed); newer pathlib is total. Guard for both.
        try:
            return (Path(legacy_sentinel_dir) / f"{task_id}.sentinel").exists()
        except OSError:
            return False
    return False


def is_parked(results_dir: Path, task_id: str) -> bool:
    """PARKED is terminal for the bridge: the disposition is already durable
    in the outbox, so the caller archives the result pair instead of looping."""
    root = result_backend(results_dir).root
    return outbox.item_status(root, task_id) == "PARKED"


def claim_for_send(results_dir: Path, task_id: str) -> Optional[ClaimToken]:
    """Publish (one-slot, idempotent) then claim. None = someone else holds
    it or it just completed — the caller skips this pass, never re-sends."""
    b = result_backend(results_dir)
    st = outbox.item_status(b.root, task_id)
    if st == "DELIVERED" or st == "PARKED":
        return None
    b.publish(task_id, b"")
    return b.claim(task_id, WORKER)


def confirm(results_dir: Path, token: ClaimToken, destination: str) -> bool:
    """CONFIRMED terminal + the one audit row per delivered result (the audit
    choke point moves here with the state, staying single)."""
    ok = result_backend(results_dir).complete(
        token, DeliveryOutcome.CONFIRMED,
        provider="discord", destination=destination)
    result_audit.record(token.item_id, "delivered", "discord")
    return ok


def failed(results_dir: Path, token: ClaimToken) -> bool:
    """NOT_DELIVERED: re-readied for the next pass; parks at the attempt cap
    so an unsendable result cannot retry forever (duplicate-generator rule)."""
    return result_backend(results_dir).complete(
        token, DeliveryOutcome.NOT_DELIVERED,
        park_at_attempts=PARK_AT_ATTEMPTS)


def failed_terminal(results_dir: Path, token: ClaimToken) -> bool:
    """One-shot semantics (bridge archives failures): note the attempt and
    park immediately — terminal and operator-visible, never a silent leak."""
    return result_backend(results_dir).complete(
        token, DeliveryOutcome.NOT_DELIVERED, park_at_attempts=1)


def unknown(results_dir: Path, token: ClaimToken) -> bool:
    """OUTCOME_UNKNOWN: the send MAY have reached Discord — park, never
    auto-retry (at-most-once bias on ambiguity, per the outbox contract)."""
    return result_backend(results_dir).complete(
        token, DeliveryOutcome.OUTCOME_UNKNOWN)
