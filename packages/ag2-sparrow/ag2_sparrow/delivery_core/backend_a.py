"""DesignAClaimBackend: the shipped outbox claim protocol behind the
ClaimBackend seam. Wrapper only — no call-site or disk-format change
(acceptance criterion 4); production callers keep using outbox.py directly
until Phase 2 routes an adapter through DeliveryCore."""
from __future__ import annotations

import hashlib
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

    capabilities = BackendCapabilities(supports_force_release=True,
                                       fresh_cycle_after_definite_park=True)

    # Only a park the broker definitely refused proves the parked body never
    # landed; any other park may hold a body the broker already accepted.
    DEFINITE_PARK_REASONS = frozenset({"permanent-refusal"})

    # Parked-body history an id may carry; a park past it saturates the id
    # closed (every new payload refused) instead of forgetting a body.
    PARKED_HISTORY_LIMIT = outbox.PARKED_HISTORY_LIMIT

    # Why the last publish for a parked id was refused; the caller's
    # quarantine line names it so an operator reads the real cause.
    REFUSALS = {
        "no-capability": "this backend admits no fresh cycle after a park",
        "park-without-payload": "the park recorded no payload to compare against",
        "parked-body-already-refused": "this body already parked on this id",
        "parked-history-saturated": "the id's parked history is full",
        "park-not-definite": "the park was not a definite refusal",
        "parked-cycle-ambiguous": "an attempt in the parked cycle may have landed",
        "attempt-unclassified": "an attempt in the parked cycle started but never classified",
        "attempt-evidence-missing": "the record does not prove every started attempt was classified "
                                    "(written before attempts were tracked, or torn)",
        "claim-live-on-park": "a live owner still holds the parked cycle's claim",
    }

    def __init__(self, root: Path, reclaim_ttl_s: float = 300.0,
                 retry_schedule: Optional[outbox.RetrySchedule] = None,
                 clock=time.time, republish_delivered: bool = True):
        self.root = Path(root)
        self.reclaim_ttl_s = reclaim_ttl_s
        self.retry_schedule = retry_schedule
        self.clock = clock
        self.republish_delivered = republish_delivered

    @staticmethod
    def _parked_digests(prior: dict) -> Optional[list]:
        """Every payload this id has parked on, oldest first; None when the
        park recorded no payload, so no new one can be proven different."""
        return outbox.parked_digests_of(prior)

    @staticmethod
    def _every_started_attempt_classified(prior: dict) -> bool:
        """True only when the record explicitly proves it: at least one attempt
        was started and exactly as many were classified by complete()."""
        if not outbox.attempt_tracking_is_valid(prior):
            return False
        started = prior.get("attempts_started")
        return started >= 1 and started == prior.get("attempts_classified")

    @staticmethod
    def _refuse(prior: dict, root: Path, item_id: str, why: str) -> bool:
        """Record why a fresh payload was refused (once per cause) and refuse."""
        if prior.get("last_refusal") != why:
            prior["last_refusal"] = why
            outbox._write_item(root, item_id, prior)
        return False

    def publish(self, item_id: str, payload: bytes, *,
                republish_delivered: Optional[bool] = None) -> bool:
        """A PARKED id accepts a payload it has never parked on only when the
        whole parked cycle is a definite refusal: its final reason is a
        permanent refusal AND no attempt in it was ambiguous (a lost response
        taints the cycle for good — a later refusal cannot prove the earlier
        body never landed). Any other park refuses every payload, because the
        broker dedupes on the envelope id and would silently keep the parked
        body in place of the new one. Every payload the id ever parked on stays
        refused, across later deliveries too, so bodies cannot alternate through
        the park; once PARKED_HISTORY_LIMIT bodies have parked, the id is
        saturated and refuses every new payload rather than forgetting one.
        An attempt that started and never classified (its owner died or raised
        mid-send) counts as ambiguous. Certainty is never inferred: the record
        must itself prove that every attempt it started was classified
        (`attempts_started` == `attempts_classified`), so a record written
        before attempts were tracked, or torn, refuses like an ambiguous one;
        a record with no counters at all (written before attempts were
        tracked) gets a sticky `attempt_evidence_missing` mark the moment it is
        touched again (begin_attempt, requeue), so the counters a later retry
        adds can never launder it; absence is never read as "never sent".
        Only a new cycle (fresh record, counters stamped at zero) clears either mark.
        A republish of a DELIVERED id starts a new cycle: the delivered cycle's
        taint does not carry over, its parked history does. Each refusal
        records its cause as `last_refusal`."""
        allow_republish = (self.republish_delivered if republish_delivered is None
                           else republish_delivered)
        text = payload.decode("utf-8", "replace")
        digest = hashlib.sha256(payload).hexdigest()
        record: dict = {}
        with outbox._item_lock(self.root, item_id):
            if outbox._item_path(self.root, item_id).exists():
                prior = outbox._read_item(self.root, item_id)
                status = prior.get("status")
                if status == "PARKED":
                    refuse = lambda why: self._refuse(prior, self.root, item_id, why)  # noqa: E731
                    if not self.capabilities.fresh_cycle_after_definite_park:
                        return refuse("no-capability")
                    parked = self._parked_digests(prior)
                    if parked is None:
                        return refuse("park-without-payload")
                    if digest in parked:
                        return refuse("parked-body-already-refused")
                    # The parked body joins the history through the one writer;
                    # a full history saturates the id closed right here.
                    if not outbox.fold_parked_digest(prior, parked[-1],
                                                     self.PARKED_HISTORY_LIMIT):
                        return refuse("parked-history-saturated")
                    if prior.get("reason") not in self.DEFINITE_PARK_REASONS:
                        return refuse("park-not-definite")
                    if prior.get("cycle_ambiguous"):
                        return refuse("parked-cycle-ambiguous")
                    if prior.get("dispatch_pending"):
                        return refuse("attempt-unclassified")
                    # Missing evidence is not evidence of safety; the sticky mark
                    # outlives the counters a later retry adds.
                    if prior.get("attempt_evidence_missing") or \
                            not self._every_started_attempt_classified(prior):
                        return refuse("attempt-evidence-missing")
                    rec = outbox.read_delivery_claim(self.root, item_id)
                    if rec is not None:
                        # The cycle ended at the park: a claim here is a crash
                        # remnant unless its owner runs or is torn; no TTL applies.
                        if not outbox._record_is_reclaimable(rec, 0.0):
                            return refuse("claim-live-on-park")
                        outbox._release_locked(self.root, item_id, rec.drainer_id)
                    record = {
                        "resend_epoch": int(prior.get("resend_epoch", 0) or 0) + 1,
                        "parked_digests": list(prior.get("parked_digests") or []),
                        "superseded_park": {
                            "attempts": int(prior.get("attempts", 0) or 0),
                            "reason": prior.get("reason"),
                            "published_at": prior.get("published_at"),
                        },
                    }
                else:
                    if status != "DELIVERED":
                        return False
                    # A caller's own policy or a live claim is not a cause the
                    # operator needs; a delivered record stays untouched for those.
                    if not allow_republish:
                        return False
                    if outbox.read_delivery_claim(self.root, item_id) is not None:
                        return False
                    # Named here so a quarantine line never carries the park's stale cause.
                    refuse = lambda why: self._refuse(prior, self.root, item_id, why)  # noqa: E731
                    history = [d for d in prior.get("parked_digests") or []
                               if isinstance(d, str)]
                    if prior.get("saturated"):
                        return refuse("parked-history-saturated")
                    if digest in history:
                        return refuse("parked-body-already-refused")
                    # A delivery in between does not forgive a parked body.
                    record = {
                        "resend_epoch": int(prior.get("resend_epoch", 0) or 0),
                        "parked_digests": history,
                    }
            record.update({
                "item_id": item_id,
                "payload": text,
                "payload_digest": digest,
                "status": "READY",
                "published_at": time.time(),
            })
            outbox.stamp_attempt_tracking(record)
            outbox._write_item(self.root, item_id, record)
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
            # A live sender owns timing too; torn claims need the grace-period sweep.
            rec = outbox.read_delivery_claim(self.root, item_id)
            recovered_torn = rec is not None and rec.state == "UNKNOWN"
            if recovered_torn:
                if not outbox._reclaim_locked(self.root, item_id, self.reclaim_ttl_s, worker):
                    return None
            elif rec is not None and not outbox.may_reclaim_delivery(
                    self.root, item_id, self.reclaim_ttl_s):
                return None
            if self.retry_schedule is not None and not outbox.retry_ready_locked(
                    self.root, item_id, self.retry_schedule, self.clock()):
                if recovered_torn:
                    outbox._release_locked(self.root, item_id, worker)
                return None
            took = (recovered_torn or outbox._acquire_locked(self.root, item_id, worker)
                    or outbox._reclaim_locked(self.root, item_id,
                                              self.reclaim_ttl_s, worker))
            if not took:
                return None
            incarnation = self._incarnation_of(item_id)
            if incarnation is None:             # record vanished under us
                return None
            return ClaimToken(item_id=item_id, worker=worker,
                              incarnation=incarnation)

    def begin_attempt(self, token: ClaimToken) -> bool:
        """Write "an attempt may dispatch" BEFORE the provider is called.

        A previous mark still present means an attempt started and never
        classified (its owner died or raised mid-send): it may have landed, so
        the cycle is tainted for good before this attempt begins."""
        item_id = token.item_id
        with outbox._item_lock(self.root, item_id):
            rec = outbox.read_delivery_claim(self.root, item_id)
            if rec is None or rec.drainer_id != token.worker or \
                    self._incarnation_of(item_id) != token.incarnation:
                return False
            item = outbox._read_item(self.root, item_id)
            if item.get("dispatch_pending"):
                item["cycle_ambiguous"] = True
            outbox.mark_untracked_attempts(item)
            item["dispatch_pending"] = True
            item["attempts_started"] = int(item.get("attempts_started", 0) or 0) + 1
            outbox._write_item(self.root, item_id, item)
            return True

    def payload_for_claim(self, token: ClaimToken) -> bytes:
        """The claimed item's original payload, including across retries/restarts."""
        with outbox._item_lock(self.root, token.item_id):
            if self._incarnation_of(token.item_id) != token.incarnation:
                raise ValueError("delivery claim no longer owns its payload")
            return outbox._read_item(self.root, token.item_id)["payload"].encode("utf-8")

    def is_terminal(self, item_id: str) -> bool:
        with outbox._item_lock(self.root, item_id):
            if not outbox._item_path(self.root, item_id).exists():
                return False
            return outbox._read_item(
                self.root, item_id).get("status") in self.TERMINAL

    def complete(self, token: ClaimToken, outcome: DeliveryOutcome,
                 park_at_attempts: Optional[int] = None,
                 provider: Optional[str] = None,
                 destination: Optional[str] = None,
                 terminal_reason: Optional[str] = None,
                 ambiguous: bool = False) -> bool:
        item_id = token.item_id
        # Validate -> transition -> retire, all under the item lock: a stale
        # incarnation must not mutate or park its successor's item.
        with outbox._item_lock(self.root, item_id):
            rec = outbox.read_delivery_claim(self.root, item_id)
            if rec is None or rec.drainer_id != token.worker or \
                    self._incarnation_of(item_id) != token.incarnation:
                return False
            if ambiguous:
                # Persisted before the outcome: the taint outlives retries,
                # restarts and an operator requeue of this same cycle.
                item = outbox._read_item(self.root, item_id)
                item["cycle_ambiguous"] = True
                outbox._write_item(self.root, item_id, item)
            if outcome is DeliveryOutcome.CONFIRMED:
                outbox.record_delivered(self.root, item_id,
                                        provider=provider, destination=destination)
            elif outcome is DeliveryOutcome.OUTCOME_UNKNOWN:
                outbox.park_item(self.root, item_id, "outcome-unknown")
            elif terminal_reason:
                outbox.note_attempt(self.root, item_id)
                outbox.park_item(self.root, item_id, terminal_reason)
            elif self.retry_schedule is not None:
                outbox.retry_failed_locked(self.root, item_id, self.clock())
            else:
                attempts = outbox.note_attempt(self.root, item_id)
                if park_at_attempts is not None and attempts >= park_at_attempts:
                    outbox.park_item(self.root, item_id, "max-attempts")
            # The pending mark outlives the outcome write: a crash before the
            # outcome is durable leaves the attempt unclassified, never clean.
            item = outbox._read_item(self.root, item_id)
            if item.pop("dispatch_pending", None):
                item["attempts_classified"] = int(item.get("attempts_classified", 0) or 0) + 1
                outbox._write_item(self.root, item_id, item)
            return outbox._release_locked(self.root, item_id, token.worker)

    def resend_epoch(self, item_id: str) -> int:
        """Operator re-send generation; 0 until a requeue bumps it."""
        return outbox.resend_epoch_for(self.root, item_id)

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
        return CleanupReport(
            pruned=0, detail="A: locks bounded by striping; parked-body history "
                             f"capped at {self.PARKED_HISTORY_LIMIT} per id (saturates closed)")

    def force_release(self, item_id: str) -> bool:
        return outbox.release_delivery_claim(self.root, item_id, force=True)
