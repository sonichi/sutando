#!/usr/bin/env python3
"""A parked item must not poison its id: a LATER result published under the
same id is a new reply and gets its own delivery cycle; re-publishing the
very payload that parked stays refused, so a rescanned live file cannot
turn one park into a retry per pass.

Run: python3 tests/delivery-core-parked-fresh-publish.test.py"""
from __future__ import annotations

import json
import subprocess
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
THIRD = b'{"id": "task-9da728fb8292936bf6", "body": "a third reply"}'
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
        self.assertIs(core.deliver_one(ITEM, SECOND).outcome, DeliveryOutcome.CONFIRMED)
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("status"), "DELIVERED")
        self.assertFalse(backend.publish(ITEM, THIRD, republish_delivered=False),
                         "the DELIVERED rule is judged in the DELIVERED state")
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("status"), "DELIVERED")
        self.assertTrue(backend.publish(ITEM, THIRD))

    def test_two_payloads_cannot_alternate_through_the_park(self):
        prov = _Provider(refusals=CAP * 2)
        backend, core = _parked(self.tmp, prov)
        self.assertTrue(backend.publish(ITEM, SECOND))
        for _ in range(CAP):
            self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.ATTEMPTED)
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("status"), "PARKED")
        calls = len(prov.calls)
        for _ in range(6):                      # the reviewer's alternation probe
            self.assertFalse(backend.publish(ITEM, FIRST), "a body that parked once is parked for good")
            self.assertIs(core.deliver_one(ITEM, FIRST).status, DrainStatus.TERMINAL)
            self.assertFalse(backend.publish(ITEM, SECOND))
            self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.TERMINAL)
        self.assertEqual(len(prov.calls), calls, "no provider call after both parks")
        self.assertEqual(backend.resend_epoch(ITEM), 1)
        self.assertTrue(backend.publish(ITEM, THIRD), "a third, never-parked body still gets its cycle")
        self.assertEqual(len(outbox._read_item(backend.root, ITEM).get("parked_digests")), 2)

    def test_a_dead_claim_left_on_a_park_does_not_refuse_the_fresh_result(self):
        prov = _Provider(refusals=CAP)
        backend, core = _parked(self.tmp, prov)
        gone = subprocess.Popen(["true"])       # a pid that no longer runs
        gone.wait()
        claim = outbox._claim_path(backend.root, ITEM)
        claim.parent.mkdir(parents=True, exist_ok=True)
        claim.write_text(json.dumps({"item_id": ITEM, "drainer_id": "crashed",
                                     "pid": gone.pid, "start_usec": None,
                                     "claimed_at": outbox.time.time()}))
        self.assertIsNotNone(outbox.read_delivery_claim(backend.root, ITEM))
        self.assertTrue(backend.publish(ITEM, SECOND),
                        "a crash between the park write and the release must not quarantine B")
        self.assertIsNone(outbox.read_delivery_claim(backend.root, ITEM), "the remnant is retired")
        self.assertIs(core.deliver_one(ITEM, SECOND).outcome, DeliveryOutcome.CONFIRMED)

    def test_a_live_claim_on_a_park_still_refuses(self):
        prov = _Provider(refusals=CAP)
        backend, _ = _parked(self.tmp, prov)
        with outbox._item_lock(backend.root, ITEM):
            self.assertTrue(outbox._acquire_locked(backend.root, ITEM, "alive"))
        self.assertFalse(backend.publish(ITEM, SECOND), "a running owner is never displaced")

    def test_a_park_that_recorded_no_payload_refuses_even_an_empty_one(self):
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        outbox._write_item(backend.root, ITEM, {"item_id": ITEM, "status": "PARKED",
                                                "attempts": CAP, "reason": "max-attempts"})
        self.assertFalse(backend.publish(ITEM, b""))
        self.assertFalse(backend.publish(ITEM, SECOND), "nothing can be proven different")
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("status"), "PARKED")

    def test_invalid_utf8_payloads_are_told_apart_by_their_bytes(self):
        prov = _Provider(refusals=CAP)
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        core = DeliveryCore(backend, prov, policy=RetryPolicy(max_attempts=CAP), worker="w1")
        self.assertTrue(backend.publish(ITEM, b"\xff"))
        for _ in range(CAP):
            core.deliver_one(ITEM, b"\xff")
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("status"), "PARKED")
        self.assertFalse(backend.publish(ITEM, b"\xff"))
        self.assertTrue(backend.publish(ITEM, b"\xfe"),
                        "two bodies that both decode to U+FFFD are still different bodies")


if __name__ == "__main__":
    unittest.main()
