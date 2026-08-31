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

    persists_receipt_metadata = True   # record_delivered() stores both

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
                # complete() writes the terminal status BEFORE releasing, so a
                # crash in that window leaves a claim no other path reclaims.
                rec = outbox.read_delivery_claim(self.root, item_id)
                if rec is not None and outbox.may_reclaim_delivery(
                        self.root, item_id, self.reclaim_ttl_s):
                    outbox._release_locked(self.root, item_id, rec.drainer_id)
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

    def is_terminal(self, item_id: str) -> bool:
        with outbox._item_lock(self.root, item_id):
            if not outbox._item_path(self.root, item_id).exists():
                return False
            return outbox._read_item(
                self.root, item_id).get("status") in self.TERMINAL

    def complete(self, token: ClaimToken, outcome: DeliveryOutcome,
                 park_at_attempts: Optional[int] = None,
                 provider: Optional[str] = None,
                 destination: Optional[str] = None) -> bool:
        item_id = token.item_id
        # Validate -> transition -> retire, all under the item lock: a stale
        # incarnation must not mutate or park its successor's item.
        with outbox._item_lock(self.root, item_id):
            rec = outbox.read_delivery_claim(self.root, item_id)
            if rec is None or rec.drainer_id != token.worker or \
                    self._incarnation_of(item_id) != token.incarnation:
                return False
            if outcome is DeliveryOutcome.CONFIRMED:
                outbox.record_delivered(self.root, item_id,
                                        provider=provider, destination=destination)
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
        """Startup reconciliation over the real claim records.

        Reports which items are reclaimable so the core can re-drive them, and
        RETIRES claims left on a TERMINAL item by a crash between complete()'s
        terminal write and its release. That crash leaves no body for the caller
        to re-derive an item id from, so claim() is never invoked for it again —
        this pass is the only path that reaches such a record.
        """
        rep = RecoverReport()
        claims = outbox._claims_dir(self.root)
        if not claims.exists():
            return rep
        for p in sorted(claims.glob("*.claim")):
            # The id comes from INSIDE the record: the filename is a lossy,
            # digest-suffixed safe key and cannot be reversed to an item id.
            rec = outbox._read_claim_at(p, "")
            if rec is None or not rec.item_id:
                continue
            item_id = rec.item_id
            with outbox._item_lock(self.root, item_id):
                current = outbox.read_delivery_claim(self.root, item_id)
                if current is None:
                    continue
                if not outbox.may_reclaim_delivery(self.root, item_id,
                                                   self.reclaim_ttl_s):
                    continue        # ALIVE or UNKNOWN owner: never displaced
                if outbox._read_item(self.root, item_id).get(
                        "status") in self.TERMINAL:
                    outbox._release_locked(self.root, item_id,
                                           current.drainer_id)
                    rep.retired.append(item_id)
                else:
                    rep.recovered.append(item_id)
        return rep

    def cleanup(self) -> CleanupReport:
        return CleanupReport(pruned=0, detail="A: bounded by lock striping")

    def force_release(self, item_id: str) -> bool:
        return outbox.release_delivery_claim(self.root, item_id, force=True)
