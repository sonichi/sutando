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
    BackendCapabilities, DeliveryCore, DeliveryOutcome, DeliveryReceipt,
    DesignAClaimBackend, ProviderCapabilities, idempotency_key)

ITEM = "room-evt-1"


def _a_backend(tmp: Path):
    return DesignAClaimBackend(tmp)


BACKENDS = {
    "A": _a_backend,
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

    def reconcile(self, item_id, idempotency_key):
        self.reconcile_calls.append((item_id, idempotency_key))
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
        out = self._core(p).deliver_one(ITEM, b"x")
        self.assertIs(out, DeliveryOutcome.CONFIRMED)
        self.assertEqual(len(p.deliver_calls), 1)

    def test_unknown_parks_without_capabilities(self):
        p = _Recorder([RuntimeError("wire died")])
        out = self._core(p).deliver_one(ITEM, b"x")
        self.assertIs(out, DeliveryOutcome.OUTCOME_UNKNOWN,
                      "UNKNOWN must never be relabeled NOT_DELIVERED")
        self.assertEqual(len(p.deliver_calls), 1, "no blind resend")

    def test_unknown_reconciles_when_capable(self):
        p = _Recorder([RuntimeError("wire died")],
                      ProviderCapabilities(reconcile_capable=True))
        out = self._core(p).deliver_one(ITEM, b"x")
        self.assertIs(out, DeliveryOutcome.CONFIRMED)
        self.assertEqual(len(p.reconcile_calls), 1)

    def test_unknown_resends_only_with_idempotent_send(self):
        p = _Recorder([RuntimeError("wire died"), DeliveryOutcome.CONFIRMED],
                      ProviderCapabilities(idempotent_send=True))
        out = self._core(p).deliver_one(ITEM, b"x")
        self.assertIs(out, DeliveryOutcome.CONFIRMED)
        self.assertEqual(len(p.deliver_calls), 2)
        self.assertEqual(p.deliver_calls[0][1], p.deliver_calls[1][1],
                         "retry must reuse the SAME idempotency key")

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
