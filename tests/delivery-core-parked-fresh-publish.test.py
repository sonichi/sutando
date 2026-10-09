#!/usr/bin/env python3
"""A parked item must not poison its id: a LATER result published under the
same id is a new reply and gets its own delivery cycle; re-publishing the
very payload that parked stays refused, so a rescanned live file cannot
turn one park into a retry per pass.

Run: python3 tests/delivery-core-parked-fresh-publish.test.py"""
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
    DeliveryCore, DeliveryOutcome, DeliveryReceipt, DesignAClaimBackend,
    DrainStatus, ProviderCapabilities, ProviderRefused, RetryPolicy)
from ag2_sparrow import outbox  # noqa: E402

ITEM = "task-9da728fb8292936bf6"
FIRST = b'{"id": "task-9da728fb8292936bf6", "body": "first reply"}'
SECOND = b'{"id": "task-9da728fb8292936bf6", "body": "a later, different reply"}'
CAP = 3


class _Provider:
    """Refuses `refusals` times, then confirms; records every call."""

    def __init__(self, refusals: int):
        self.refusals = refusals
        self.capabilities = ProviderCapabilities()
        self.calls: list[tuple[str, bytes, str]] = []

    def deliver(self, item_id, payload, idempotency_key):
        self.calls.append((item_id, bytes(payload), idempotency_key))
        if self.refusals > 0:
            self.refusals -= 1
            raise ProviderRefused("relay refused")
        return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED)

    def reconcile(self, attempt):
        return None


def _parked(tmp: Path, provider: _Provider):
    """Backend A with ITEM driven to PARKED at the attempt cap."""
    backend = DesignAClaimBackend(tmp / ".outbox")
    core = DeliveryCore(backend, provider, policy=RetryPolicy(max_attempts=CAP),
                        worker="w1")
    assert backend.publish(ITEM, FIRST)
    for _ in range(CAP):
        res = core.deliver_one(ITEM, FIRST)
        assert res.status is DrainStatus.ATTEMPTED, res
    assert outbox._read_item(backend.root, ITEM).get("status") == "PARKED"
    return backend, core


class ParkedIdAcceptsAFreshResult(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_the_same_payload_stays_refused_after_the_park(self):
        prov = _Provider(refusals=CAP)
        backend, core = _parked(self.tmp, prov)
        calls = len(prov.calls)
        for _ in range(5):                      # five drain passes over one live file
            self.assertFalse(backend.publish(ITEM, FIRST),
                             "the payload that parked must not re-enter the queue")
            self.assertIs(core.deliver_one(ITEM, FIRST).status, DrainStatus.TERMINAL)
        self.assertEqual(len(prov.calls), calls, "a park is final for that payload")
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("status"), "PARKED")

    def test_a_different_payload_gets_its_own_cycle(self):
        prov = _Provider(refusals=CAP)
        backend, core = _parked(self.tmp, prov)
        parked_keys = {k for _, _, k in prov.calls}
        self.assertTrue(backend.publish(ITEM, SECOND),
                        "a later result for a parked id is a new publication")
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual(rec.get("status"), "READY")
        self.assertEqual(int(rec.get("attempts", 0)), 0, "the budget starts over")
        self.assertEqual(rec.get("superseded_park", {}).get("attempts"), CAP,
                         "the park it replaced stays on the record")
        res = core.deliver_one(ITEM, SECOND)
        self.assertIs(res.status, DrainStatus.ATTEMPTED, "one POST, not a quarantine")
        self.assertIs(res.outcome, DeliveryOutcome.CONFIRMED)
        self.assertEqual(prov.calls[-1][1], SECOND, "the new body went out")
        self.assertNotIn(prov.calls[-1][2], parked_keys,
                         "a new logical send: the key must not dedupe against the park")
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("status"), "DELIVERED")

    def test_a_fresh_result_that_fails_parks_again_without_looping(self):
        prov = _Provider(refusals=CAP * 2)
        backend, core = _parked(self.tmp, prov)
        self.assertTrue(backend.publish(ITEM, SECOND))
        for _ in range(CAP):
            self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.ATTEMPTED)
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("status"), "PARKED")
        calls = len(prov.calls)
        for _ in range(5):
            self.assertFalse(backend.publish(ITEM, SECOND))
            self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.TERMINAL)
        self.assertEqual(len(prov.calls), calls)

    def test_a_delivered_id_is_unchanged_by_this_rule(self):
        prov = _Provider(refusals=0)
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        core = DeliveryCore(backend, prov, policy=RetryPolicy(max_attempts=CAP), worker="w1")
        self.assertTrue(backend.publish(ITEM, FIRST))
        self.assertIs(core.deliver_one(ITEM, FIRST).outcome, DeliveryOutcome.CONFIRMED)
        self.assertTrue(backend.publish(ITEM, SECOND), "DELIVERED republishes as before")
        self.assertFalse(backend.publish(ITEM, SECOND, republish_delivered=False))


if __name__ == "__main__":
    unittest.main()
