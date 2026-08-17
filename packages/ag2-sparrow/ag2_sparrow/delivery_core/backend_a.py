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
        if outbox._item_path(self.root, item_id).exists():
            return False
        outbox._write_item(self.root, item_id, {
            "item_id": item_id,
            "payload": payload.decode("utf-8", "replace"),
            "status": "READY",
            "published_at": time.time(),
        })
        return True

    def claim(self, item_id: str, worker: str) -> Optional[ClaimToken]:
        if not outbox._item_path(self.root, item_id).exists():
            return None
        if outbox.acquire_delivery_claim(self.root, item_id, worker):
            return ClaimToken(item_id=item_id, opaque=worker)
        if outbox.reclaim_delivery_claim(self.root, item_id,
                                         self.reclaim_ttl_s, worker):
            return ClaimToken(item_id=item_id, opaque=worker)
        return None

    def complete(self, token: ClaimToken, outcome: DeliveryOutcome) -> bool:
        if outcome is DeliveryOutcome.CONFIRMED:
            d = outbox._read_item(self.root, token.item_id)
            d["status"] = "DELIVERED"
            outbox._write_item(self.root, token.item_id, d)
        elif outcome is DeliveryOutcome.OUTCOME_UNKNOWN:
            outbox.park_item(self.root, token.item_id, "outcome-unknown")
        else:
            outbox.note_attempt(self.root, token.item_id)
        return outbox.release_delivery_claim(self.root, token.item_id,
                                             token.opaque)

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
