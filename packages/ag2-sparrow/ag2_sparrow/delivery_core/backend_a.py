"""DesignAClaimBackend: the shipped outbox claim protocol behind the
ClaimBackend seam. Wrapper only — no call-site or disk-format change
(acceptance criterion 4); production callers keep using outbox.py directly
until Phase 2 routes an adapter through DeliveryCore."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .. import outbox
from .contract import (BackendCapabilities, ClaimToken, CleanupReport,
                       DeliveryOutcome, RecoverReport)


class DesignAClaimBackend:
    """A: free-standing claim records + flock-serialized transitions.
    Reclaim TTL is A's dead-owner recovery window; force-release exists
    (declared) as the administrative-destruction mechanism."""

    capabilities = BackendCapabilities(supports_force_release=True)

    def __init__(self, root: Path, reclaim_ttl_s: float = 300.0):
        self.root = Path(root)
        self.reclaim_ttl_s = reclaim_ttl_s

    def publish(self, item_id: str, payload: bytes) -> bool:
        with outbox._item_lock(self.root, item_id):
            if outbox._item_path(self.root, item_id).exists():
                # DELIVERED = completed lifecycle -> fresh cycle (C-parity);
                # PARKED stays refused: the operator holds it.
                if outbox._read_item(self.root, item_id).get("status") != "DELIVERED":
                    return False
                if outbox.read_delivery_claim(self.root, item_id) is not None:
                    return False
            outbox._write_item(self.root, item_id, {
                "item_id": item_id,
                "payload": payload.decode("utf-8", "replace"),
                "status": "READY",
                "published_at": time.time(),
            })
            return True

    def _incarnation_of(self, item_id: str) -> Optional[str]:
        """The claim record's non-reusable identity: pid + process birth +
        claim stamp. A restarted worker of the same name cannot reproduce
        it, which is what makes a stale token detectable."""
        rec = outbox.read_delivery_claim(self.root, item_id)
        if rec is None or rec.state == "UNKNOWN":
            return None
        return f"{rec.drainer_id}:{rec.pid}:{rec.start_usec}:{rec.claimed_at}"

    TERMINAL = {"DELIVERED", "PARKED"}

    def claim(self, item_id: str, worker: str) -> Optional[ClaimToken]:
        # Eligibility, acquisition and capture in ONE critical section: a
        # later capture can adopt a successor's incarnation.
        with outbox._item_lock(self.root, item_id):
            if not outbox._item_path(self.root, item_id).exists():
                return None
            if outbox._read_item(self.root, item_id).get("status") in self.TERMINAL:
                return None
            took = (outbox._acquire_locked(self.root, item_id, worker)
                    or outbox._reclaim_locked(self.root, item_id,
                                              self.reclaim_ttl_s, worker))
            if not took:
                return None
            incarnation = self._incarnation_of(item_id)
            if incarnation is None:             # record vanished under us
                return None
            return ClaimToken(item_id=item_id, worker=worker,
                              incarnation=incarnation)

    def complete(self, token: ClaimToken, outcome: DeliveryOutcome,
                 park_at_attempts: Optional[int] = None) -> bool:
        item_id = token.item_id
        # Validate -> transition -> retire, all under the item lock: a stale
        # incarnation must not mutate or park its successor's item.
        with outbox._item_lock(self.root, item_id):
            rec = outbox.read_delivery_claim(self.root, item_id)
            if rec is None or rec.drainer_id != token.worker or \
                    self._incarnation_of(item_id) != token.incarnation:
                return False
            if outcome is DeliveryOutcome.CONFIRMED:
                d = outbox._read_item(self.root, item_id)
                d["status"] = "DELIVERED"
                outbox._write_item(self.root, item_id, d)
            elif outcome is DeliveryOutcome.OUTCOME_UNKNOWN:
                outbox.park_item(self.root, item_id, "outcome-unknown")
            else:
                attempts = outbox.note_attempt(self.root, item_id)
                if park_at_attempts is not None and attempts >= park_at_attempts:
                    outbox.park_item(self.root, item_id, "max-attempts")
            return outbox._release_locked(self.root, item_id, token.worker)

    def attempts(self, item_id: str) -> int:
        return outbox.attempts_for(self.root, item_id)

    def park(self, item_id: str, reason: str) -> None:
        outbox.park_item(self.root, item_id, reason)

    def recover(self) -> RecoverReport:
        """A recovers lazily at claim time (reclaim TTL); this pass reports
        which items are currently reclaimable so the core can re-drive
        them. Nothing is moved — reclaim happens under the next claim."""
        rep = RecoverReport()
        claims = outbox._claims_dir(self.root)
        if claims.exists():
            for p in claims.glob("*.json"):
                item_id = p.stem
                if outbox.may_reclaim_delivery(self.root, item_id,
                                               self.reclaim_ttl_s):
                    rep.recovered.append(item_id)
        return rep

    def cleanup(self) -> CleanupReport:
        return CleanupReport(pruned=0, detail="A: bounded by lock striping")

    def force_release(self, item_id: str) -> bool:
        return outbox.release_delivery_claim(self.root, item_id, force=True)
