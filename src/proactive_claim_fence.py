"""Proactive claim lifecycle on the outbox ClaimBackend seam.

File renames stay the inter-bridge visibility mechanism (a claimed file leaves
every ``*.txt`` glob peers poll); the backend replaces the in-memory transient
attempt counter and pid-scoped orphan recovery with durable, incarnation-fenced
records. Ordering per operation: file move first, backend second — ``recover()``
reconciles a crash between the two.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

try:  # pragma: no cover - package context (ag2-sparrow bundles the siblings)
    from .proactive_recovery import recover_orphan_sending_files, release_claim
    from .send_failure_policy import decide_failed_send
except ImportError:  # pragma: no cover - flat src/ import path
    from proactive_recovery import recover_orphan_sending_files, release_claim
    from send_failure_policy import decide_failed_send

try:
    from .delivery.readiness import retire_claim_if_unchanged
except ImportError:
    from delivery.readiness import retire_claim_if_unchanged  # noqa: E402

from ag2_sparrow.delivery_core import DeliveryOutcome


class ProactiveClaimFence:
    """Claim/confirm/retry/park for one bridge's proactive files.

    Every transition pairs one file move with one backend record transition.
    The fence never blocks delivery: a backend refusal degrades that cycle to
    file-only claiming (logged), because losing durability for one item is
    recoverable and losing the owner's message is not.
    """

    def __init__(self, backend, results_dir: Path, worker: str = "proactive"):
        self._backend = backend
        self._results_dir = results_dir
        self._worker = worker
        self._tokens: dict = {}  # claim path -> (item_id, ClaimToken|None)

    @staticmethod
    def _item_id(path: Path) -> str:
        # mtime-scoped identity: a re-written body is a fresh delivery cycle,
        # never a continuation of the consumed one's attempt history.
        return f"{path.name}#{path.stat().st_mtime_ns}"

    def claim(self, path: Path) -> Optional[Path]:
        """Claim ``path`` for delivery; returns the ``.sending`` claim path."""
        # Only the proactive family is ever claimed by rename — a task result
        # renamed here would vanish from its own consumer's glob.
        if not path.name.startswith("proactive-"):
            return None
        try:
            item = self._item_id(path)
        except FileNotFoundError:
            return None
        claim = path.with_suffix(".sending")
        try:
            path.rename(claim)
        except FileNotFoundError:
            return None
        token = None
        try:
            self._backend.publish(item, b"")  # False = record exists; claim decides
            token = self._backend.claim(item, self._worker)
            if token is None:
                self._backend.recover()
                token = self._backend.claim(item, self._worker)
        except Exception as exc:  # never lose the body: degrade to file-only
            print(f"  [fence] backend claim degraded for {claim.name}: {exc}",
                  flush=True)
        if token is None:
            print(f"  [fence] {claim.name}: no backend claim this cycle "
                  "(file-only)", flush=True)
        self._tokens[claim] = (item, token)
        return claim

    def confirm(self, claim: Path, delivered: "str | None" = None) -> bool:
        """Delivery confirmed. Given the delivered body, the claim is retired
        only while it still holds exactly that body; a claim that grew is
        released so a later pass sends it whole (bytes are never destroyed).
        Without a body (a terminal drop) the file is consumed outright."""
        if delivered is None:
            claim.unlink(missing_ok=True)
            self._finish(claim, DeliveryOutcome.CONFIRMED)
            return True
        if retire_claim_if_unchanged(claim, delivered):
            self._finish(claim, DeliveryOutcome.CONFIRMED)
            return True
        print(f"  [fence] {claim.name} grew after the send; released for a whole resend",
              flush=True)
        release_claim(claim)
        self._finish(claim, DeliveryOutcome.NOT_DELIVERED)
        return False

    def drop(self, claim: Path, reason: str) -> None:
        """Terminal non-delivery discard (e.g. no resolvable recipient)."""
        print(f"  [fence] dropping {claim.name}: {reason}", flush=True)
        self.confirm(claim)

    def release(self, claim: Path) -> bool:
        """Non-attempt release (body unready / destined for another bridge).

        The record completes NOT_DELIVERED; if nothing here ever re-claims it
        (a peer bridge delivers the file), backend cleanup ages the stale
        ready record out. attempts() inflation is confined to this item's
        mtime identity, which a re-written body replaces.
        """
        released = release_claim(claim)
        self._finish(claim, DeliveryOutcome.NOT_DELIVERED)
        return released

    def attempts(self, claim: Path) -> int:
        item, _token = self._tokens.get(claim, (None, None))
        if item is None:
            return 0
        try:
            return self._backend.attempts(item)
        except Exception:  # degraded cycle: no durable count to report
            return 0

    def fail(self, claim: Path, exc: BaseException, progressed: bool,
             undelivered_dir: Optional[Path] = None) -> str:
        """Execute the send-failure decision. Returns retried/parked/stuck."""
        decision = decide_failed_send(exc, self.attempts(claim), progressed)
        if decision == "retry":
            if release_claim(claim):
                # NOT_DELIVERED both re-arms the record and durably counts the
                # attempt — the counter the old in-memory dict lost on restart.
                self._finish(claim, DeliveryOutcome.NOT_DELIVERED)
                return "retried"
            # A newer .txt body exists; park this one rather than clobber it.
        item, token = self._tokens.pop(claim, (None, None))
        body_name = claim.with_suffix(".txt").name
        try:
            undelivered = undelivered_dir if undelivered_dir is not None \
                else claim.parent / "undelivered"
            undelivered.mkdir(parents=True, exist_ok=True)
            claim.rename(undelivered / body_name)
        except Exception:
            self._tokens[claim] = (item, token)
            return "stuck"
        if token is not None:
            try:
                self._backend.complete(token, DeliveryOutcome.NOT_DELIVERED)
                self._backend.park(item, "partial-delivery" if progressed
                                   else "permanent-failure")
            except Exception as e:
                print(f"  [fence] park record failed for {body_name}: {e}",
                      flush=True)
        return "parked"

    def recover(self) -> int:
        """Startup reconciliation: backend half then file half.

        backend.recover() re-arms records whose claimant incarnation died and
        quarantines ghosts; the file sweep restores ``.sending`` orphans to the
        polling stream. A record with no file (crash between confirm-unlink and
        complete) goes stale-ready and is aged out by backend cleanup.
        """
        try:
            self._backend.recover()
        except Exception as exc:
            print(f"  [fence] backend recover failed: {exc}", flush=True)
        return recover_orphan_sending_files(self._results_dir)

    def _finish(self, claim: Path, outcome) -> None:
        _item, token = self._tokens.pop(claim, (None, None))
        if token is None:
            return
        try:
            self._backend.complete(token, outcome)
        except Exception as exc:
            print(f"  [fence] backend complete failed for {claim.name}: {exc}",
                  flush=True)
