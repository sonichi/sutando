#!/usr/bin/env python3
"""Delivery Core contract suite — backend-agnostic: every registered
ClaimBackend fixture runs the SAME tests (acceptance criterion 1). A is
wired; B and Discord-legacy join via their adapters (Phases 3/4) by adding
a fixture to BACKENDS, changing no test.

Enforcement-suite slots (001, same PR): cross-incarnation key-identity
machine op; static no-claim-material-in-key scan; lease/local seam oracle;
fence fault tests.

Run: python3 tests/delivery-core-contract.test.py"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow.delivery_core import (  # noqa: E402
    BackendCapabilities, ClaimToken, DeliveryCore, DeliveryOutcome, DeliveryReceipt,
    DesignAClaimBackend, DrainStatus, ProviderCapabilities,
    ProviderIndeterminate, ProviderRefused, RetryPolicy, idempotency_key)
from ag2_sparrow.delivery_core.backend_c import DesignCClaimBackend  # noqa: E402
from ag2_sparrow import outbox  # noqa: E402

ITEM = "room-evt-1"


def _a_backend(tmp: Path):
    return DesignAClaimBackend(tmp)


def _c_backend(tmp: Path):
    return DesignCClaimBackend(tmp, activate=True)


BACKENDS = {
    "A": _a_backend,
    "C": _c_backend,
    # "B": Phase-4 plug-in (exp/design-b-eval driver shape)
    # "discord-legacy": Phase-3 adapter over rename claim + sentinels
}


class _Recorder:
    """Provider double: scripted receipts, recorded calls."""

    def __init__(self, outcomes, caps=ProviderCapabilities()):
        self.outcomes = list(outcomes)
        self.capabilities = caps
        self.deliver_calls = []
        self.reconcile_calls = []

    def deliver(self, item_id, payload, idempotency_key):
        self.deliver_calls.append((item_id, idempotency_key))
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return DeliveryReceipt(outcome=out)

    def reconcile(self, attempt):
        self.reconcile_calls.append((attempt.item_id, attempt.idempotency_key))
        return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED,
                               provider_ref="r-1")


class ContractCase(unittest.TestCase):
    """Runs once per BACKENDS entry (see load_tests)."""
    backend_name = "A"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="delivery-core-")
        self.backend = BACKENDS[self.backend_name](Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_requeueable_backend_mints_its_own_resend_epoch(self):
        """Pin the coupling, not the spelling (per sonichi on #3853).

        `_resend_epoch` falls back to 0 for a backend without `resend_epoch`,
        and 0 keeps the idempotency key at `id#0` — so the provider dedupes an
        operator re-send against the very attempt that parked the item, while
        the operator reads `result: requeued`. A bare `hasattr` assertion would
        fail today for a reason that is not a defect: the one backend lacking
        the method is also the one `outbox` cannot see, so no epoch is ever
        minted on it. What must hold is the COUPLING — reachable by outbox
        implies mints an epoch — which is inert now and fires the day a second
        backend gains an outbox-backed store.
        """
        root = Path(self.tmp.name)
        self.backend.publish(ITEM, b"x")
        reachable = any(r.get("item_id") == ITEM for r in outbox.list_items(root))
        if not reachable:
            self.skipTest("outbox cannot reach this backend's items, so it "
                          "mints no epoch — the exposure is unreachable here")
        self.assertTrue(
            callable(getattr(self.backend, "resend_epoch", None)),
            "outbox can requeue this backend's items, so it must mint a resend "
            "epoch; without one the key stays id#0 and the re-send is deduped "
            "against the parked attempt")

    def test_capabilities_are_declared_not_sniffed(self):
        self.assertIsInstance(self.backend.capabilities, BackendCapabilities)

    def test_single_owner(self):
        self.backend.publish(ITEM, b"x")
        t1 = self.backend.claim(ITEM, "w1")
        self.assertIsNotNone(t1)
        self.assertIsNone(self.backend.claim(ITEM, "w2"),
                          "second live claim must lose")

    def test_publish_refuses_live_id(self):
        self.assertTrue(self.backend.publish(ITEM, b"x"))
        self.assertFalse(self.backend.publish(ITEM, b"x"),
                         "same-id publish while live must refuse (CE-1 class)")

    def test_complete_retires_ownership(self):
        self.backend.publish(ITEM, b"x")
        t = self.backend.claim(ITEM, "w1")
        self.assertTrue(self.backend.complete(t, DeliveryOutcome.CONFIRMED))

    def test_incarnation_identifies_its_owner(self):
        """The owner check in complete() reads as a second, independent
        validation. It is not: this incarnation format embeds drainer_id,
        so incarnation equality already implies owner equality, and
        deleting the owner check leaves the suite green.

        Pin the coupling rather than the check. If a future incarnation
        stops naming its owner, this fails and says so — at which point
        the owner check becomes load-bearing instead of redundant."""
        self.backend.publish(ITEM, b"x")
        t = self.backend.claim(ITEM, "w1")
        self.assertIsNotNone(t)
        self.assertIn("w1", t.incarnation,
                      "incarnation no longer identifies its owner — the "
                      "owner check in complete() is now the only thing "
                      "separating two workers, so give it its own control")

    def test_a_foreign_worker_cannot_complete_a_live_claim(self):
        """Defense in depth for the branch above: a token naming another
        worker must be refused even when its incarnation is current. Only
        constructible by hand — claim() cannot emit an inconsistent token,
        which is exactly why the branch needs a control of its own."""
        import ag2_sparrow.outbox as outbox
        self.backend.publish(ITEM, b"x")
        live = self.backend.claim(ITEM, "w1")
        forged = ClaimToken(item_id=live.item_id, worker="w2",
                            incarnation=live.incarnation)
        before = outbox._read_item(self.backend.root, ITEM).get("status")
        self.assertFalse(self.backend.complete(forged, DeliveryOutcome.CONFIRMED),
                         "a foreign worker completed another worker's claim")
        self.assertEqual(outbox._read_item(self.backend.root, ITEM).get("status"),
                         before, "a refused completion must change nothing")
        self.assertTrue(self.backend.complete(live, DeliveryOutcome.CONFIRMED),
                        "the real owner must still be able to complete")

    def test_stale_incarnation_completes_nothing(self):
        """Owner review finding 1+2: a token that outlived its claim must
        change NOTHING. Validating after mutating lets a dead incarnation
        park or advance its successor's item."""
        self.backend.publish(ITEM, b"x")
        stale = self.backend.claim(ITEM, "w1")
        self.backend.force_release(ITEM)          # w1's claim is gone
        successor = self.backend.claim(ITEM, "w2")
        self.assertIsNotNone(successor)
        self.assertNotEqual(stale.incarnation, successor.incarnation,
                            "incarnation must not be reproducible by name")
        import ag2_sparrow.outbox as outbox
        before = outbox._read_item(self.backend.root, ITEM).get("status")
        self.assertFalse(
            self.backend.complete(stale, DeliveryOutcome.OUTCOME_UNKNOWN),
            "stale token must not complete")
        # The transition each outcome performs is the observable, not the
        # return value: parking is what a stale UNKNOWN would inflict.
        self.assertEqual(
            outbox._read_item(self.backend.root, ITEM).get("status"), before,
            "stale token parked the successor's item")
        self.assertTrue(
            self.backend.complete(successor, DeliveryOutcome.CONFIRMED),
            "the live incarnation still owns the claim")

    def test_a_foreign_item_is_never_retired(self):
        """Retirement authority belongs to the dispatching consumer: a
        backend must not complete an item it never issued a token for.
        results/ is shared, so filename shape is not ownership (#3018)."""
        self.backend.publish(ITEM, b"x")
        foreign = ClaimToken(item_id=ITEM, worker="another-consumer",
                             incarnation="another-consumer:1:1:1")
        self.assertFalse(self.backend.complete(foreign,
                                               DeliveryOutcome.CONFIRMED),
                         "a token this backend never issued retired the item")
        import ag2_sparrow.outbox as outbox
        self.assertNotEqual(
            outbox._read_item(self.backend.root, ITEM).get("status"),
            "DELIVERED", "a foreign token marked the item delivered")

    def test_force_release_is_capability_gated(self):
        caps = self.backend.capabilities
        if caps.supports_force_release:
            self.backend.publish(ITEM, b"x")
            self.backend.claim(ITEM, "w1")
            self.assertTrue(self.backend.force_release(ITEM))
        else:
            with self.assertRaises(NotImplementedError,
                                   msg="non-declaring backend must raise, "
                                       "never silently no-op"):
                self.backend.force_release(ITEM)


class CorePolicy(unittest.TestCase):
    """DeliveryCore outcome policy — backend-independent (criterion 2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="delivery-core-")
        self.backend = DesignAClaimBackend(Path(self.tmp.name))
        self.backend.publish(ITEM, b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def _core(self, provider):
        return DeliveryCore(self.backend, provider)

    def test_confirmed_is_terminal(self):
        p = _Recorder([DeliveryOutcome.CONFIRMED])
        r = self._core(p).deliver_one(ITEM, b"x")
        self.assertIs(r.outcome, DeliveryOutcome.CONFIRMED)
        self.assertIs(r.status, DrainStatus.ATTEMPTED)
        self.assertEqual(len(p.deliver_calls), 1)

    def test_unknown_parks_without_capabilities(self):
        p = _Recorder([ProviderIndeterminate("timeout after send")])
        r = self._core(p).deliver_one(ITEM, b"x")
        self.assertIs(r.outcome, DeliveryOutcome.OUTCOME_UNKNOWN,
                      "UNKNOWN must never be relabeled NOT_DELIVERED")
        self.assertEqual(len(p.deliver_calls), 1, "no blind resend")

    def test_unknown_reconciles_when_capable(self):
        p = _Recorder([ProviderIndeterminate("timeout after send")],
                      ProviderCapabilities(reconcile_capable=True))
        r = self._core(p).deliver_one(ITEM, b"x")
        self.assertIs(r.outcome, DeliveryOutcome.CONFIRMED)
        self.assertEqual(len(p.reconcile_calls), 1)

    def test_reconcile_by_safe_resend_uses_the_attempt_payload(self):
        """The case the (item_id, key) signature could not express: a
        keyed-dedup provider whose only receipt store IS the send. Its
        reconcile re-offers the attempt's own payload under the attempt's
        own key — possible only because the attempt object carries both."""
        class _ResendReconciler(_Recorder):
            def reconcile(self, attempt):
                self.reconcile_calls.append(
                    (attempt.item_id, attempt.idempotency_key))
                return self.deliver(attempt.item_id, attempt.payload,
                                    attempt.idempotency_key)
        p = _ResendReconciler(
            [ProviderIndeterminate("timeout after send"),
             DeliveryOutcome.CONFIRMED],
            ProviderCapabilities(reconcile_capable=True))
        r = self._core(p).deliver_one(ITEM, b"the-body")
        self.assertIs(r.outcome, DeliveryOutcome.CONFIRMED)
        self.assertEqual(len(p.reconcile_calls), 1)
        # both offers carried the SAME derived key: dedup identity held.
        self.assertEqual(p.deliver_calls[0], p.deliver_calls[1])

    def test_reconcile_resend_that_stays_ambiguous_reaches_UNKNOWN(self):
        """reconcile() may re-send, so its typed failures are the SAME
        taxonomy as deliver()'s. Unclassified they escape deliver_one before
        backend.complete(), leaving the claim owned and attempts unchanged."""
        class _AmbiguousReconciler(_Recorder):
            def reconcile(self, attempt):
                self.reconcile_calls.append(attempt.item_id)
                raise ProviderIndeterminate("timeout on the reconcile re-send")
        p = _AmbiguousReconciler([ProviderIndeterminate("timeout after send")],
                                 ProviderCapabilities(reconcile_capable=True))
        core = self._core(p)
        r = core.deliver_one(ITEM, b"x")
        self.assertIs(r.outcome, DeliveryOutcome.OUTCOME_UNKNOWN,
                      "a second ambiguity is still ambiguity — park, do not relabel")
        self.assertIs(r.status, DrainStatus.ATTEMPTED,
                      "the item reached a transition; it is not stuck behind a live claim")
        self.assertEqual(len(p.reconcile_calls), 1)

    def test_reconcile_REFUSED_must_not_relabel_the_original_ambiguity(self):
        """A refused reconcile says the SECOND call never dispatched. It says
        nothing about a first send that may already have crossed the boundary.

        This case previously asserted NOT_DELIVERED and so pinned the bug:
        complete() would count a retry, leave the item READY, and a later
        drain re-sends — a duplicate user-visible delivery whenever the
        original send actually succeeded (idempotent_send=False permits it).
        """
        class _RefusingReconciler(_Recorder):
            def reconcile(self, attempt):
                self.reconcile_calls.append(attempt.item_id)
                raise ProviderRefused("payload rejected on the re-send")
        p = _RefusingReconciler([ProviderIndeterminate("timeout after send")],
                                ProviderCapabilities(reconcile_capable=True))
        r = self._core(p).deliver_one(ITEM, b"x")
        self.assertIs(r.outcome, DeliveryOutcome.OUTCOME_UNKNOWN,
                      "a refused reconcile is not evidence about the first send")
        self.assertIs(r.status, DrainStatus.ATTEMPTED)
        self.assertEqual(len(p.reconcile_calls), 1)

    def test_reconcile_RECEIPT_of_not_delivered_MAY_resolve_the_ambiguity(self):
        """The discriminator: a receipt is a positive statement ABOUT the
        original attempt, so it alone may replace OUTCOME_UNKNOWN."""
        class _ReportingReconciler(_Recorder):
            def reconcile(self, attempt):
                self.reconcile_calls.append(attempt.item_id)
                return DeliveryReceipt(outcome=DeliveryOutcome.NOT_DELIVERED,
                                       detail="server never accepted it")
        p = _ReportingReconciler([ProviderIndeterminate("timeout after send")],
                                 ProviderCapabilities(reconcile_capable=True))
        r = self._core(p).deliver_one(ITEM, b"x")
        self.assertIs(r.outcome, DeliveryOutcome.NOT_DELIVERED,
                      "an explicit receipt DOES resolve the original attempt")

    def test_lost_response_then_refused_reconcile_does_not_redeliver(self):
        """End-to-end against the REAL DesignA backend: the case the unit
        assertions above only describe. Before the fix this drained twice and
        the user saw the message twice."""
        class _LostThenRefused:
            capabilities = ProviderCapabilities(reconcile_capable=True,
                                                idempotent_send=False)

            def __init__(self):
                self.effects = 0
                self.n = 0

            def deliver(self, item_id, payload, key):
                self.n += 1
                self.effects += 1          # the side effect HAS happened
                if self.n == 1:
                    raise ProviderIndeterminate("response lost after send")
                return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED,
                                       provider_ref="duplicate")

            def reconcile(self, attempt):
                raise ProviderRefused("refused before dispatch")

        root = Path(tempfile.mkdtemp()) / ".outbox"
        backend = DesignAClaimBackend(root)
        prov = _LostThenRefused()
        core = DeliveryCore(backend, prov, policy=RetryPolicy(max_attempts=3),
                            worker="w1")
        backend.publish(ITEM, b"body")
        first = core.deliver_one(ITEM, b"body")
        self.assertIs(first.outcome, DeliveryOutcome.OUTCOME_UNKNOWN)
        self.assertEqual(outbox._read_item(root, ITEM).get("status"), "PARKED",
                         "ambiguity parks for a human; it does not stay READY")
        second = core.deliver_one(ITEM, b"body")
        self.assertIs(second.status, DrainStatus.TERMINAL,
                      "a parked item is not re-drained")
        self.assertIsNot(second.status, DrainStatus.NOT_CLAIMED,
                         "and it is not contention: NOT_CLAIMED invites the "
                         "caller to retry an item no pass can ever claim")
        self.assertEqual(prov.effects, 1,
                         "exactly one user-visible delivery, not two")

    def test_reconcile_untyped_error_still_propagates(self):
        """Only the typed taxonomy is classified. A programming error must
        stay loud rather than be laundered into a delivery outcome."""
        class _BrokenReconciler(_Recorder):
            def reconcile(self, attempt):
                raise KeyError("config key missing")
        p = _BrokenReconciler([ProviderIndeterminate("timeout after send")],
                              ProviderCapabilities(reconcile_capable=True))
        with self.assertRaises(KeyError):
            self._core(p).deliver_one(ITEM, b"x")

    def test_unknown_resends_only_with_idempotent_send(self):
        p = _Recorder([ProviderIndeterminate("timeout after send"),
                       DeliveryOutcome.CONFIRMED],
                      ProviderCapabilities(idempotent_send=True))
        r = self._core(p).deliver_one(ITEM, b"x")
        self.assertIs(r.outcome, DeliveryOutcome.CONFIRMED)
        self.assertEqual(len(p.deliver_calls), 2)
        self.assertEqual(p.deliver_calls[0][1], p.deliver_calls[1][1],
                         "retry must reuse the SAME idempotency key")

    def test_lost_race_is_not_a_delivery_outcome(self):
        """Owner review finding 3: no provider call was made, so nothing
        external is ambiguous — reporting UNKNOWN pollutes telemetry and
        recovery policy."""
        self.backend.claim(ITEM, "someone-else")   # item already owned
        p = _Recorder([])
        r = self._core(p).deliver_one(ITEM, b"x")
        self.assertIs(r.status, DrainStatus.NOT_CLAIMED)
        self.assertIsNone(r.outcome, "a lost race has no delivery outcome")
        self.assertEqual(p.deliver_calls, [], "provider must not be called")

    def test_refusal_is_not_delivered_not_unknown(self):
        """Owner review finding 5: only boundary-crossing failures are
        UNKNOWN. A refusal before dispatch is a definite non-delivery."""
        p = _Recorder([ProviderRefused("rejected before dispatch")])
        r = self._core(p).deliver_one(ITEM, b"x")
        self.assertIs(r.outcome, DeliveryOutcome.NOT_DELIVERED)

    def test_programming_error_propagates(self):
        """Owner review finding 5, the other half: a bare Exception is a
        bug or a config error, not evidence the side effect may have run.
        Swallowing it as UNKNOWN parks the item and hides the defect."""
        p = _Recorder([TypeError("payload is not bytes")])
        with self.assertRaises(TypeError):
            self._core(p).deliver_one(ITEM, b"x")

    def test_retry_policy_parks_at_the_ceiling(self):
        """Owner review finding 4: RetryPolicy was declared but unread."""
        core = DeliveryCore(self.backend, _Recorder([]),
                            RetryPolicy(max_attempts=2))
        for _ in range(2):
            core.provider = _Recorder([DeliveryOutcome.NOT_DELIVERED])
            core.deliver_one(ITEM, b"x")
        import ag2_sparrow.outbox as outbox
        self.assertEqual(
            outbox._read_item(self.backend.root, ITEM).get("status"), "PARKED",
            "at max_attempts the core must park, not retry forever")

    def _second_drain_calls(self, first_outcome, policy=None):
        """Drive once to `first_outcome`, then again; return the SECOND
        drain's provider-call count. Terminal states must make it zero."""
        core = DeliveryCore(self.backend, _Recorder([first_outcome]),
                            policy or RetryPolicy())
        core.deliver_one(ITEM, b"x")
        second = _Recorder([DeliveryOutcome.CONFIRMED])
        core.provider = second
        core.deliver_one(ITEM, b"x")
        return len(second.deliver_calls)

    def test_delivered_item_is_not_delivered_again(self):
        self.assertEqual(self._second_drain_calls(DeliveryOutcome.CONFIRMED), 0,
                         "a DELIVERED item was delivered twice")

    def test_parked_unknown_is_not_redriven(self):
        p = _Recorder([ProviderIndeterminate("timeout after send")])
        core = DeliveryCore(self.backend, p)
        core.deliver_one(ITEM, b"x")
        second = _Recorder([DeliveryOutcome.CONFIRMED])
        core.provider = second
        core.deliver_one(ITEM, b"x")
        self.assertEqual(len(second.deliver_calls), 0,
                         "an OUTCOME_UNKNOWN item parked for reconcile was "
                         "immediately re-driven")

    def test_item_parked_at_the_ceiling_is_not_attempted_again(self):
        self.assertEqual(
            self._second_drain_calls(DeliveryOutcome.NOT_DELIVERED,
                                     RetryPolicy(max_attempts=1)), 0,
            "an item parked at the retry ceiling was attempted again")

    def test_stale_claimant_cannot_adopt_a_successors_incarnation(self):
        """Acquisition and capture must be one critical section, or A
        returns a token carrying B's incarnation."""
        import ag2_sparrow.outbox as outbox
        self.backend.publish(ITEM, b"x")
        # Interleave a successor between acquisition and capture; one
        # critical section makes this impossible.
        real_incarnation_of = self.backend._incarnation_of
        fired = []

        def interleave(item_id):
            if not fired:
                fired.append(True)
                try:
                    # Blocked under the fix: the lock is still held.
                    self.backend.force_release(item_id)
                    self.backend.claim(item_id, "B")
                except Exception:
                    pass
            return real_incarnation_of(item_id)

        self.backend._incarnation_of = interleave
        try:
            a = self.backend.claim(ITEM, "A")
        finally:
            self.backend._incarnation_of = real_incarnation_of
        self.assertTrue(fired, "the interleave never ran — test is vacuous")
        if a is not None:
            # Discriminator: a token must name its own worker's
            # incarnation; the racy structure gives A the successor's.
            self.assertTrue(a.incarnation.startswith(f"{a.worker}:"),
                            f"claim returned a token for {a.worker!r} carrying "
                            f"another incarnation: {a.incarnation!r}")

    def test_successor_confirmation_survives_a_stale_ceiling_park(self):
        """The ceiling transition must ride WITH the completion. Parked
        after the claim is released, a successor can claim and CONFIRM in
        the gap and the stale caller's park then overwrites DELIVERED —
        losing delivery evidence and making a requeue duplicate the send."""
        import ag2_sparrow.outbox as outbox
        core = DeliveryCore(self.backend, _Recorder([DeliveryOutcome.NOT_DELIVERED]),
                            RetryPolicy(max_attempts=1))
        real_complete, fired, confirmed = self.backend.complete, [], []

        def racing_complete(token, outcome, park_at_attempts=None, **kw):
            ok = real_complete(token, outcome, park_at_attempts, **kw)
            if not fired:                     # the gap: successor gets in
                fired.append(True)
                t2 = self.backend.claim(ITEM, "successor")
                if t2 is not None:
                    confirmed.append(real_complete(t2, DeliveryOutcome.CONFIRMED))
            return ok

        self.backend.complete = racing_complete
        try:
            core.deliver_one(ITEM, b"x")
        finally:
            self.backend.complete = real_complete
        self.assertTrue(fired, "the interleave never ran — test is vacuous")
        # Under the fix the ceiling park is atomic, so the successor cannot
        # claim at all; the defect is a confirmation that is then ERASED.
        if any(confirmed):
            self.assertEqual(
                outbox._read_item(self.backend.root, ITEM).get("status"),
                "DELIVERED",
                "a stale ceiling park overwrote the successor's confirmed delivery")

    def test_key_never_contains_claim_material(self):
        self.assertEqual(idempotency_key(ITEM), idempotency_key(ITEM),
                         "stable across calls")
        self.assertNotIn("delivery-core", idempotency_key(ITEM),
                         "no worker identity in the key")


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in BACKENDS:
        case = type(f"ContractCase_{name}", (ContractCase,),
                    {"backend_name": name})
        suite.addTests(loader.loadTestsFromTestCase(case))
    suite.addTests(loader.loadTestsFromTestCase(CorePolicy))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
