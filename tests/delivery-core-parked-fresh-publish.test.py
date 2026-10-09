#!/usr/bin/env python3
"""A parked item must not poison its id, but only when the park proves the
parked body never landed: after a DEFINITE refusal a later, different result
published under the same id gets its own delivery cycle; after an ambiguous
park (lost responses, exhausted retries, outcome-unknown) every payload stays
refused, because the broker dedupes on the envelope id and would keep the
parked body while reporting the new one delivered. Every payload the id ever
parked on stays refused, across a later delivery and an operator requeue too; a
cycle with any ambiguous attempt stays refused even when its last attempt was a
definite refusal; and the parked history is bounded, saturating the id closed.

Run: python3 tests/delivery-core-parked-fresh-publish.test.py"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow.delivery_core import (  # noqa: E402
    BackendCapabilities, DeliveryCore, DeliveryOutcome, DeliveryReceipt, DesignAClaimBackend,
    DrainStatus, ProviderCapabilities, ProviderIndeterminate, ProviderRefused,
    ProviderPermanentRefused, RetryPolicy)
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceResultProvider  # noqa: E402
from ag2_sparrow import outbox  # noqa: E402

ITEM = "task-9da728fb8292936bf6"
FIRST = b'{"id": "task-9da728fb8292936bf6", "body": "first reply"}'
SECOND = b'{"id": "task-9da728fb8292936bf6", "body": "a later, different reply"}'
THIRD = b'{"id": "task-9da728fb8292936bf6", "body": "a third reply"}'
CAP = 3


class _Provider:
    """Refuses `refusals` times, then confirms; records every call.
    `permanent` makes each refusal a definite one (the broker declined)."""

    def __init__(self, refusals: int, permanent: bool = True):
        self.refusals = refusals
        self.permanent = permanent
        self.capabilities = ProviderCapabilities()
        self.calls: list[tuple[str, bytes, str]] = []

    def deliver(self, item_id, payload, idempotency_key):
        self.calls.append((item_id, bytes(payload), idempotency_key))
        if self.refusals > 0:
            self.refusals -= 1
            if self.permanent:
                raise ProviderPermanentRefused("relay declined")
            raise ProviderRefused("relay refused")
        return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED)

    def reconcile(self, attempt):
        return None


def _core(tmp: Path, provider):
    backend = DesignAClaimBackend(tmp / ".outbox")
    return backend, DeliveryCore(backend, provider, policy=RetryPolicy(max_attempts=CAP),
                                 worker="w1")


def _parked(tmp: Path, provider: _Provider, payload: bytes = FIRST):
    """Backend A with ITEM driven to PARKED by the provider's refusals."""
    backend, core = _core(tmp, provider)
    assert backend.publish(ITEM, payload)
    while outbox._read_item(backend.root, ITEM).get("status") != "PARKED":
        res = core.deliver_one(ITEM, payload)
        assert res.status is DrainStatus.ATTEMPTED, res
    return backend, core


def _status(backend):
    return outbox._read_item(backend.root, ITEM).get("status")


class ParkedIdAcceptsAFreshResultAfterADefiniteRefusal(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_the_same_payload_stays_refused_after_the_park(self):
        prov = _Provider(refusals=1)
        backend, core = _parked(self.tmp, prov)
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("reason"), "permanent-refusal")
        calls = len(prov.calls)
        for _ in range(5):                      # five drain passes over one live file
            self.assertFalse(backend.publish(ITEM, FIRST),
                             "the payload that parked must not re-enter the queue")
            self.assertIs(core.deliver_one(ITEM, FIRST).status, DrainStatus.TERMINAL)
        self.assertEqual(len(prov.calls), calls, "a park is final for that payload")
        self.assertEqual(_status(backend), "PARKED")

    def test_a_different_payload_gets_its_own_cycle(self):
        prov = _Provider(refusals=1)
        backend, core = _parked(self.tmp, prov)
        parked_keys = {k for _, _, k in prov.calls}
        self.assertTrue(backend.publish(ITEM, SECOND),
                        "a later result for a definitely refused id is a new publication")
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual(rec.get("status"), "READY")
        self.assertEqual(int(rec.get("attempts", 0)), 0, "the budget starts over")
        self.assertEqual(rec.get("superseded_park", {}).get("reason"), "permanent-refusal",
                         "the park it replaced stays on the record")
        res = core.deliver_one(ITEM, SECOND)
        self.assertIs(res.status, DrainStatus.ATTEMPTED, "one POST, not a quarantine")
        self.assertIs(res.outcome, DeliveryOutcome.CONFIRMED)
        self.assertEqual(prov.calls[-1][1], SECOND, "the new body went out")
        self.assertNotIn(prov.calls[-1][2], parked_keys,
                         "a new logical send: the key must not dedupe against the park")
        self.assertEqual(_status(backend), "DELIVERED")

    def test_an_ambiguous_park_refuses_every_payload(self):
        for reason, provider in (
                ("max-attempts", _Provider(refusals=CAP, permanent=False)),
                ("outcome-unknown", None)):
            with self.subTest(reason=reason):
                tmp = Path(tempfile.mkdtemp())
                if provider is not None:
                    backend, core = _parked(tmp, provider)
                else:
                    backend, core = _core(tmp, _Provider(refusals=0))
                    self.assertTrue(backend.publish(ITEM, FIRST))
                    backend.park(ITEM, "outcome-unknown")
                self.assertEqual(outbox._read_item(backend.root, ITEM).get("reason"), reason)
                self.assertFalse(backend.publish(ITEM, SECOND),
                                 "the parked body may have landed: a successor would be "
                                 "deduped away by the broker and reported delivered")
                self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.TERMINAL,
                              "the caller quarantines it where an operator can see it")
                self.assertEqual(_status(backend), "PARKED")

    def test_a_retry_window_exhausted_park_refuses_a_fresh_payload(self):
        backend, core = _core(self.tmp, _Provider(refusals=0))
        self.assertTrue(backend.publish(ITEM, FIRST))
        backend.park(ITEM, "retry-window-exhausted")
        self.assertFalse(backend.publish(ITEM, SECOND))
        self.assertEqual(_status(backend), "PARKED")

    def test_a_fresh_result_that_fails_parks_again_without_looping(self):
        prov = _Provider(refusals=2)
        backend, core = _parked(self.tmp, prov)
        self.assertTrue(backend.publish(ITEM, SECOND))
        self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.ATTEMPTED)
        self.assertEqual(_status(backend), "PARKED")
        calls = len(prov.calls)
        for _ in range(5):
            self.assertFalse(backend.publish(ITEM, SECOND))
            self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.TERMINAL)
        self.assertEqual(len(prov.calls), calls)

    def test_a_delivered_id_is_unchanged_by_this_rule(self):
        prov = _Provider(refusals=0)
        backend, core = _core(self.tmp, prov)
        self.assertTrue(backend.publish(ITEM, FIRST))
        self.assertIs(core.deliver_one(ITEM, FIRST).outcome, DeliveryOutcome.CONFIRMED)
        self.assertTrue(backend.publish(ITEM, SECOND), "DELIVERED republishes as before")
        self.assertIs(core.deliver_one(ITEM, SECOND).outcome, DeliveryOutcome.CONFIRMED)
        self.assertEqual(_status(backend), "DELIVERED")
        self.assertFalse(backend.publish(ITEM, THIRD, republish_delivered=False),
                         "the DELIVERED rule is judged in the DELIVERED state")
        self.assertEqual(_status(backend), "DELIVERED")
        self.assertTrue(backend.publish(ITEM, THIRD))

    def test_a_delivered_id_with_a_live_claim_refuses(self):
        prov = _Provider(refusals=0)
        backend, core = _core(self.tmp, prov)
        self.assertTrue(backend.publish(ITEM, FIRST))
        self.assertIs(core.deliver_one(ITEM, FIRST).outcome, DeliveryOutcome.CONFIRMED)
        with outbox._item_lock(backend.root, ITEM):
            self.assertTrue(outbox._acquire_locked(backend.root, ITEM, "finishing"))
        self.assertFalse(backend.publish(ITEM, SECOND),
                         "a delivered item still held by its finisher is not republished")

    def test_two_payloads_cannot_alternate_through_the_park(self):
        prov = _Provider(refusals=2)
        backend, core = _parked(self.tmp, prov)
        self.assertTrue(backend.publish(ITEM, SECOND))
        self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.ATTEMPTED)
        self.assertEqual(_status(backend), "PARKED")
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

    def test_a_delivery_in_between_does_not_forgive_a_parked_body(self):
        """refuse(A), confirm(B), refuse(C): A must stay refused (the reviewer's
        delivered_reset probe), and C's park keeps A in its history."""
        prov = _Provider(refusals=1)
        backend, core = _parked(self.tmp, prov)          # A parks (definite)
        self.assertTrue(backend.publish(ITEM, SECOND))     # B
        self.assertIs(core.deliver_one(ITEM, SECOND).outcome, DeliveryOutcome.CONFIRMED)
        self.assertEqual(_status(backend), "DELIVERED")
        self.assertFalse(backend.publish(ITEM, FIRST), "A was parked; a delivery of B does not clear it")
        prov.refusals = 1
        prov.permanent = True
        self.assertTrue(backend.publish(ITEM, THIRD))      # C
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual(rec.get("resend_epoch"), 1, "the epoch survives the delivered republish")
        self.assertIs(core.deliver_one(ITEM, THIRD).status, DrainStatus.ATTEMPTED)
        self.assertEqual(_status(backend), "PARKED")
        self.assertFalse(backend.publish(ITEM, FIRST), "A is still refused after C parked")
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("parked_digests"),
                         [hashlib.sha256(FIRST).hexdigest()],
                         "C's park carries A's digest")
        self.assertEqual(len(DesignAClaimBackend._parked_digests(
            outbox._read_item(backend.root, ITEM))), 2)

    def test_rotating_many_bodies_never_readmits_one(self):
        """34 distinct bodies cycled three times (the reviewer's cap_rotation
        probe): each is accepted exactly once, and none is accepted again."""
        bodies = [json.dumps({"id": ITEM, "n": n}).encode() for n in range(34)]
        prov = _Provider(refusals=len(bodies) * 3)
        backend, core = _core(self.tmp, prov)
        accepted = 0
        for _ in range(3):
            for body in bodies:
                if backend.publish(ITEM, body):
                    accepted += 1
                    self.assertIs(core.deliver_one(ITEM, body).status, DrainStatus.ATTEMPTED)
                    self.assertEqual(_status(backend), "PARKED")
        self.assertEqual(accepted, len(bodies), "every body gets one cycle, none a second")
        self.assertEqual(len(prov.calls), len(bodies))
        self.assertEqual(len(outbox._read_item(backend.root, ITEM).get("parked_digests")),
                         len(bodies) - 1)

    def test_a_dead_claim_left_on_a_park_does_not_refuse_the_fresh_result(self):
        prov = _Provider(refusals=1)
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
        prov = _Provider(refusals=1)
        backend, _ = _parked(self.tmp, prov)
        with outbox._item_lock(backend.root, ITEM):
            self.assertTrue(outbox._acquire_locked(backend.root, ITEM, "alive"))
        self.assertFalse(backend.publish(ITEM, SECOND), "a running owner is never displaced")

    def test_a_torn_claim_on_a_park_refuses(self):
        """An unreadable claim names no owner; like every reclaim path, publish
        never steals it — the fresh result is quarantined, visibly, not lost."""
        prov = _Provider(refusals=1)
        backend, core = _parked(self.tmp, prov)
        claim = outbox._claim_path(backend.root, ITEM)
        claim.parent.mkdir(parents=True, exist_ok=True)
        claim.write_text("{not json")
        self.assertEqual(outbox.read_delivery_claim(backend.root, ITEM).state, "UNKNOWN")
        self.assertFalse(backend.publish(ITEM, SECOND))
        self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.TERMINAL)
        self.assertEqual(_status(backend), "PARKED")

    def test_a_park_that_recorded_no_payload_refuses_even_an_empty_one(self):
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        outbox._write_item(backend.root, ITEM, {"item_id": ITEM, "status": "PARKED",
                                                "attempts": 1, "reason": "permanent-refusal"})
        self.assertFalse(backend.publish(ITEM, b""))
        self.assertFalse(backend.publish(ITEM, SECOND), "nothing can be proven different")
        self.assertEqual(_status(backend), "PARKED")

    def test_a_legacy_park_without_a_digest_is_judged_by_its_stored_text(self):
        """Records written before payload digests existed carry only the text."""
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        outbox._write_item(backend.root, ITEM, {"item_id": ITEM, "status": "PARKED",
                                                "attempts": 1, "reason": "permanent-refusal",
                                                "payload": FIRST.decode("utf-8")})
        self.assertFalse(backend.publish(ITEM, FIRST), "the stored text is the parked body")
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("last_refusal"),
                         "parked-body-already-refused", "judged by the text's digest")
        # A fresh body is refused too, but for the record's missing attempt
        # evidence, never for being mistaken for the parked one.
        self.assertFalse(backend.publish(ITEM, SECOND))
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("last_refusal"),
                         "attempt-evidence-missing")

    def test_invalid_utf8_payloads_are_told_apart_by_their_bytes(self):
        prov = _Provider(refusals=1)
        backend, core = _core(self.tmp, prov)
        self.assertTrue(backend.publish(ITEM, b"\xff"))
        core.deliver_one(ITEM, b"\xff")
        self.assertEqual(_status(backend), "PARKED")
        self.assertFalse(backend.publish(ITEM, b"\xff"))
        self.assertTrue(backend.publish(ITEM, b"\xfe"),
                        "two bodies that both decode to U+FFFD are still different bodies")


class _Broker:
    """A request double for the real AG2 Space provider: dedupes by envelope
    id and keeps the FIRST body it stored, like the gateway it stands in for.
    mode: 'lose' stores then loses the response; 'accept' stores or dedupes;
    'refuse' declines without storing; 'http:503' stores then answers 503;
    'http:409' declines with a 409 without storing. `script` runs one mode per
    POST, then falls back to `mode`."""

    def __init__(self, mode: str, script: list[str] | None = None):
        self.mode = mode
        self.script = list(script or [])
        self.stored: dict[str, dict] = {}
        self.posts: list[dict] = []

    def _http(self, code: int):
        return urllib.error.HTTPError("http://broker/v1/results", code, "scripted", {},
                                      io.BytesIO(b""))

    def request(self, method, path, payload):
        self.posts.append(payload)
        mode = self.script.pop(0) if self.script else self.mode
        if mode == "refuse":
            return {"ok": False, "error": "declined"}
        if mode == "http:409":
            raise self._http(409)
        duplicate = payload["id"] in self.stored
        if not duplicate:
            self.stored[payload["id"]] = payload
        if mode == "lose":
            raise urllib.error.URLError("response lost after the broker stored it")
        if mode == "http:503":
            raise self._http(503)
        return {"ok": True, "duplicate": duplicate}


class ThroughTheRealProvider(unittest.TestCase):
    """The reviewers' recipe: Backend A + DeliveryCore(defer_idempotent_resend)
    + the real AG2SpaceResultProvider against a deduping broker."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _core(self, broker):
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        core = DeliveryCore(backend, AG2SpaceResultProvider(broker.request),
                            policy=RetryPolicy(max_attempts=CAP, defer_idempotent_resend=True),
                            worker="gateway-result-drain")
        return backend, core

    def test_after_an_ambiguous_park_the_broker_still_holds_a_and_b_is_not_reported_delivered(self):
        broker = _Broker("lose")
        backend, core = self._core(broker)
        self.assertTrue(backend.publish(ITEM, FIRST))
        for _ in range(CAP):
            self.assertIs(core.deliver_one(ITEM, FIRST).status, DrainStatus.ATTEMPTED)
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual((rec.get("status"), rec.get("reason")), ("PARKED", "max-attempts"))
        self.assertEqual(broker.stored[ITEM]["body"], "first reply", "A landed; only its receipts were lost")
        broker.mode = "accept"                   # from now on the broker dedupes by id
        self.assertFalse(backend.publish(ITEM, SECOND),
                         "B must not start a cycle the broker would dedupe into A")
        self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.TERMINAL)
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("status"), "PARKED",
                         "B is not reported DELIVERED while the broker holds A")
        self.assertEqual(broker.stored[ITEM]["body"], "first reply")
        self.assertEqual(len(broker.posts), CAP, "no POST for B")

    def test_after_a_definite_refusal_b_is_delivered(self):
        broker = _Broker("refuse")
        backend, core = self._core(broker)
        self.assertTrue(backend.publish(ITEM, FIRST))
        res = core.deliver_one(ITEM, FIRST)
        self.assertIs(res.status, DrainStatus.ATTEMPTED)
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual((rec.get("status"), rec.get("reason")), ("PARKED", "permanent-refusal"))
        self.assertNotIn(ITEM, broker.stored, "a definite refusal stores nothing")
        broker.mode = "accept"
        self.assertTrue(backend.publish(ITEM, SECOND))
        self.assertIs(core.deliver_one(ITEM, SECOND).outcome, DeliveryOutcome.CONFIRMED)
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("status"), "DELIVERED")
        self.assertEqual(broker.stored[ITEM]["body"], "a later, different reply")
        self.assertEqual(len(broker.posts), 2)


class TheWholeCycleMustBeDefinite(unittest.TestCase):
    """A refusal on the LAST attempt proves nothing about an earlier one that
    may have crossed the boundary: lost-then-409, lost-then-ok:false and
    503-then-refusal all park as permanent-refusal, and all must keep refusing
    a successor (the reviewers' mixed-cycle probes)."""

    MIXED = {"lost-then-409": ["lose", "http:409"],
             "lost-then-ok-false": ["lose", "refuse"],
             "503-then-refusal": ["http:503", "refuse"]}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _core(self, broker, root=None):
        backend = DesignAClaimBackend(root or self.tmp / ".outbox")
        core = DeliveryCore(backend, AG2SpaceResultProvider(broker.request),
                            policy=RetryPolicy(max_attempts=CAP, defer_idempotent_resend=True),
                            worker="gateway-result-drain")
        return backend, core

    def _mixed_park(self, script):
        broker = _Broker("accept", script=script)
        backend, core = self._core(broker)
        self.assertTrue(backend.publish(ITEM, FIRST))
        first = core.deliver_one(ITEM, FIRST)
        self.assertIs(first.outcome, DeliveryOutcome.NOT_DELIVERED,
                      "the lost response is folded into a retryable attempt")
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual(rec.get("status"), "READY")
        self.assertTrue(rec.get("cycle_ambiguous"),
                        "the taint is written before the fold, while the cycle is still live")
        self.assertIs(core.deliver_one(ITEM, FIRST).status, DrainStatus.ATTEMPTED)
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual((rec.get("status"), rec.get("reason")), ("PARKED", "permanent-refusal"))
        return broker, backend, core

    def test_a_mixed_cycle_keeps_refusing_a_successor(self):
        for name, script in self.MIXED.items():
            with self.subTest(cycle=name):
                self.tmp = Path(tempfile.mkdtemp())
                broker, backend, core = self._mixed_park(script)
                self.assertEqual(broker.stored.get(ITEM, {}).get("body"), "first reply",
                                 "A landed on the first attempt; only its response was lost")
                self.assertFalse(backend.publish(ITEM, SECOND),
                                 "a later refusal does not prove the earlier attempt never landed")
                self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.TERMINAL)
                rec = outbox._read_item(backend.root, ITEM)
                self.assertEqual(rec.get("status"), "PARKED", "B is quarantined, not DELIVERED")
                self.assertEqual(broker.stored[ITEM]["body"], "first reply")
                self.assertEqual(len(broker.posts), 2, "no POST for B")

    def test_the_taint_survives_a_restart(self):
        broker, backend, _ = self._mixed_park(self.MIXED["lost-then-ok-false"])
        broker.mode = "accept"
        restarted, core = self._core(broker, root=backend.root)      # fresh objects, same disk
        self.assertFalse(restarted.publish(ITEM, SECOND))
        self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.TERMINAL)
        self.assertEqual(len(broker.posts), 2)

    def test_the_taint_survives_an_operator_requeue_of_the_same_cycle(self):
        broker, backend, core = self._mixed_park(self.MIXED["lost-then-ok-false"])
        self.assertIs(outbox.requeue_item(backend.root, ITEM, operator="op"),
                      outbox.RequeueOutcome.REQUEUED)
        broker.script = ["refuse"]
        self.assertIs(core.deliver_one(ITEM, FIRST).status, DrainStatus.ATTEMPTED)
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual((rec.get("status"), rec.get("reason")), ("PARKED", "permanent-refusal"))
        self.assertFalse(backend.publish(ITEM, SECOND),
                         "the explicit retry's refusal still cannot clear the earlier ambiguity")

    def test_an_untainted_definite_refusal_still_admits_a_fresh_cycle(self):
        broker = _Broker("accept", script=["refuse"])
        backend, core = self._core(broker)
        self.assertTrue(backend.publish(ITEM, FIRST))
        self.assertIs(core.deliver_one(ITEM, FIRST).status, DrainStatus.ATTEMPTED)
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual((rec.get("status"), rec.get("reason")), ("PARKED", "permanent-refusal"))
        self.assertFalse(rec.get("cycle_ambiguous", False))
        self.assertTrue(backend.publish(ITEM, SECOND))
        self.assertFalse(outbox._read_item(backend.root, ITEM).get("cycle_ambiguous", False),
                         "a fresh cycle starts untainted")
        self.assertIs(core.deliver_one(ITEM, SECOND).outcome, DeliveryOutcome.CONFIRMED)
        self.assertEqual(broker.stored[ITEM]["body"], "a later, different reply")


_CHILD_DEATH = r"""
import os, sys, json, pathlib
sys.path.insert(0, sys.argv[1])
from ag2_sparrow.delivery_core import DesignAClaimBackend, DeliveryCore, RetryPolicy
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceResultProvider
root, store, item, body = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), sys.argv[4], sys.argv[5]
def request(method, path, payload):
    store.write_text(json.dumps(payload))        # the broker stored A ...
    os._exit(17)                                 # ... and this owner died mid-POST
backend = DesignAClaimBackend(root, reclaim_ttl_s=0.0)
core = DeliveryCore(backend, AG2SpaceResultProvider(request),
                    policy=RetryPolicy(max_attempts=3, defer_idempotent_resend=True),
                    worker="gateway-result-drain")
assert backend.publish(item, body.encode())
core.deliver_one(item, body.encode())
"""


class AStartedAttemptThatNeverClassifiesTaintsTheCycle(unittest.TestCase):
    """The taint used to be written in complete(), after the provider call, so
    an attempt that landed A and then died or raised left no mark; a later
    definite refusal parked the cycle as untainted and admitted B, which the
    broker deduped into A. The started-attempt mark is written BEFORE the send
    and only a classified completion clears it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _core(self, broker):
        backend = DesignAClaimBackend(self.tmp / ".outbox", reclaim_ttl_s=0.0)
        core = DeliveryCore(backend, AG2SpaceResultProvider(broker.request),
                            policy=RetryPolicy(max_attempts=CAP, defer_idempotent_resend=True),
                            worker="gateway-result-drain")
        return backend, core

    def _then_refused_and_b_is_not_admitted(self, broker, backend, core, posts_before):
        broker.script = ["refuse"]                   # the retry of A is definitely refused
        self.assertIs(core.deliver_one(ITEM, FIRST).status, DrainStatus.ATTEMPTED)
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual((rec.get("status"), rec.get("reason")), ("PARKED", "permanent-refusal"))
        self.assertTrue(rec.get("cycle_ambiguous"),
                        "the attempt that never classified taints the cycle")
        self.assertFalse(rec.get("dispatch_pending"), "the classified attempt cleared its mark")
        broker.mode = "accept"
        self.assertFalse(backend.publish(ITEM, SECOND),
                         "a later refusal cannot prove the dead attempt never landed")
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("last_refusal"),
                         "parked-cycle-ambiguous")
        self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.TERMINAL)
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("status"), "PARKED")
        self.assertEqual(broker.stored[ITEM]["body"], "first reply", "the broker holds A")
        self.assertEqual(len(broker.posts), posts_before + 1, "no POST for B")

    def test_an_owner_that_dies_during_the_post_taints_the_cycle(self):
        store = self.tmp / "broker-store.json"
        proc = subprocess.run([sys.executable, "-c", _CHILD_DEATH, str(_PKG),
                               str(self.tmp / ".outbox"), str(store), ITEM, FIRST.decode()],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 17, proc.stderr[-400:])
        self.assertEqual(json.loads(store.read_text())["body"], "first reply",
                         "the broker stored A before the owner died")
        broker = _Broker("accept")
        broker.stored[ITEM] = json.loads(store.read_text())   # the broker's memory survives
        backend, core = self._core(broker)
        rec = outbox._read_item(backend.root, ITEM)
        self.assertTrue(rec.get("dispatch_pending"), "the dead attempt's mark is on the record")
        self._then_refused_and_b_is_not_admitted(broker, backend, core, 0)

    def _raising_after_store(self, exc):
        broker = _Broker("accept")
        real = broker.request
        broker.torn = True

        def request(method, path, payload):
            resp = real(method, path, payload)       # the broker stored A ...
            if broker.torn:
                raise exc                            # ... then the response tore
            return resp
        broker.request = request
        return broker, real

    def test_a_torn_response_is_indeterminate_not_a_crash(self):
        for name, exc in (("IncompleteRead", http.client.IncompleteRead(b"")),
                          ("garbled 200", json.JSONDecodeError("garbled", "<html>", 0))):
            with self.subTest(failure=name):
                self.tmp = Path(tempfile.mkdtemp())
                broker, real = self._raising_after_store(exc)
                backend, core = self._core(broker)
                self.assertTrue(backend.publish(ITEM, FIRST))
                try:
                    res = core.deliver_one(ITEM, FIRST)
                except Exception:                # noqa: BLE001 - the pre-fix shape
                    backend.force_release(ITEM)  # the owner's claim outlives the raise
                else:
                    self.assertIs(res.outcome, DeliveryOutcome.NOT_DELIVERED,
                                  "an unclassified post-send failure is folded like a lost response")
                broker.torn = False
                self._then_refused_and_b_is_not_admitted(broker, backend, core, 1)

    def test_a_programming_error_after_the_send_still_leaves_the_mark(self):
        """The core lets a non-taxonomy error propagate, loudly; the record
        still says an attempt started, so no later refusal can admit B."""
        class _Broken:
            capabilities = ProviderCapabilities(idempotent_send=True)

            def deliver(self, item_id, payload, key):
                raise KeyError("config key missing")

            def reconcile(self, attempt):
                return None
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        core = DeliveryCore(backend, _Broken(), policy=RetryPolicy(max_attempts=CAP), worker="w1")
        self.assertTrue(backend.publish(ITEM, FIRST))
        with self.assertRaises(KeyError):
            core.deliver_one(ITEM, FIRST)
        self.assertTrue(outbox._read_item(backend.root, ITEM).get("dispatch_pending"))
        backend.force_release(ITEM)
        prov = _Provider(refusals=1)
        core = DeliveryCore(backend, prov, policy=RetryPolicy(max_attempts=CAP), worker="w1")
        self.assertIs(core.deliver_one(ITEM, FIRST).status, DrainStatus.ATTEMPTED)
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual((rec.get("status"), rec.get("reason")), ("PARKED", "permanent-refusal"))
        self.assertTrue(rec.get("cycle_ambiguous"))
        self.assertFalse(backend.publish(ITEM, SECOND))

    def test_a_park_written_over_a_pending_attempt_admits_nothing(self):
        """The park's reason is definite, but an attempt that started and
        never classified is still on the record: the cycle is not definite."""
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        self.assertTrue(backend.publish(ITEM, FIRST))
        token = backend.claim(ITEM, "w1")
        self.assertTrue(backend.begin_attempt(token))      # the owner then died mid-send
        backend.force_release(ITEM)
        backend.park(ITEM, "permanent-refusal")            # a sweep or operator parks it
        rec = outbox._read_item(backend.root, ITEM)
        self.assertTrue(rec.get("dispatch_pending"))
        self.assertFalse(rec.get("cycle_ambiguous", False))
        self.assertFalse(backend.publish(ITEM, SECOND),
                         "a started attempt without a classification is not a definite cycle")
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("last_refusal"),
                         "attempt-unclassified")

    def test_a_classified_attempt_leaves_no_pending_mark(self):
        prov = _Provider(refusals=0)
        backend, core = _core(self.tmp, prov)
        self.assertTrue(backend.publish(ITEM, FIRST))
        self.assertIs(core.deliver_one(ITEM, FIRST).outcome, DeliveryOutcome.CONFIRMED)
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual(rec.get("attempts_started"), 1)
        self.assertNotIn("dispatch_pending", rec)
        self.assertNotIn("cycle_ambiguous", rec)

    def test_a_reconcile_receipt_clears_the_ambiguity(self):
        """A receipt is a statement about the original attempt: a cycle whose
        ambiguity was RESOLVED by one carries no taint (the mutant that taints
        on every reconcile fails here)."""
        class _Resolved:
            capabilities = ProviderCapabilities(reconcile_capable=True)

            def __init__(self):
                self.n = 0

            def deliver(self, item_id, payload, key):
                self.n += 1
                raise ProviderIndeterminate("response lost after send")

            def reconcile(self, attempt):
                return DeliveryReceipt(outcome=DeliveryOutcome.NOT_DELIVERED,
                                       detail="server never accepted it")
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        core = DeliveryCore(backend, _Resolved(), policy=RetryPolicy(max_attempts=CAP), worker="w1")
        self.assertTrue(backend.publish(ITEM, FIRST))
        self.assertIs(core.deliver_one(ITEM, FIRST).outcome, DeliveryOutcome.NOT_DELIVERED)
        rec = outbox._read_item(backend.root, ITEM)
        self.assertNotIn("cycle_ambiguous", rec, "the receipt resolved the ambiguity")
        self.assertNotIn("dispatch_pending", rec)


class HistoryIsBounded(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_the_parked_history_saturates_closed_instead_of_forgetting(self):
        limit = DesignAClaimBackend.PARKED_HISTORY_LIMIT
        prov = _Provider(refusals=10 ** 6)
        backend, core = _core(self.tmp, prov)
        accepted = 0
        for n in range(2000):
            body = json.dumps({"id": ITEM, "n": n}).encode()
            if backend.publish(ITEM, body):
                accepted += 1
                self.assertIs(core.deliver_one(ITEM, body).status, DrainStatus.ATTEMPTED)
        self.assertEqual(accepted, limit + 1, "the retained history plus the body whose park "
                         "saturated it; nothing beyond")
        rec = outbox._read_item(backend.root, ITEM)
        self.assertTrue(rec.get("saturated"))
        self.assertEqual(rec.get("status"), "PARKED", "the item stays visible to the operator")
        self.assertEqual(len(rec.get("parked_digests")), limit, "the stored history is bounded")
        self.assertEqual(len(DesignAClaimBackend._parked_digests(rec)), limit + 1,
                         "the parked body itself is still refused, from its own digest")
        size = outbox._item_path(backend.root, ITEM).stat().st_size
        self.assertLess(size, 64 * (limit + 1) + 4096, f"record grew to {size} bytes")
        self.assertFalse(backend.publish(ITEM, b'{"id": "x", "fresh": true}'),
                         "saturated: every new payload is refused")
        self.assertIs(core.deliver_one(ITEM, b'{"id": "x", "fresh": true}').status,
                      DrainStatus.TERMINAL)
        self.assertEqual(len(prov.calls), limit + 1)
        self.assertIn(str(limit), backend.cleanup().detail)

    def test_requeue_and_delivery_fold_through_the_same_writer(self):
        """The reviewers' loop: publish, park, requeue, deliver, 140 times.
        Each park folds into the id's history through the one writer, so the
        id saturates at the limit and the next different body is refused."""
        limit = DesignAClaimBackend.PARKED_HISTORY_LIMIT
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        accepted = 0
        for n in range(limit + 12):
            body = json.dumps({"id": ITEM, "n": n}).encode()
            if not backend.publish(ITEM, body):
                break
            accepted += 1
            backend.park(ITEM, "permanent-refusal")
            self.assertIs(outbox.requeue_item(backend.root, ITEM, operator="op"),
                          outbox.RequeueOutcome.REQUEUED)
            outbox.record_delivered(backend.root, ITEM)
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual(accepted, limit + 1, "the limit-th park saturates; one more cycle "
                         "was already live when it did")
        self.assertTrue(rec.get("saturated"), "requeue cannot grow the history past the bound")
        self.assertEqual(len(rec.get("parked_digests")), limit)
        self.assertFalse(backend.publish(ITEM, b'{"id": "x", "fresh": true}'))

    def test_each_refusal_names_its_cause(self):
        prov = _Provider(refusals=1)
        backend, core = _parked(self.tmp, prov)
        self.assertFalse(backend.publish(ITEM, FIRST))
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("last_refusal"),
                         "parked-body-already-refused")
        backend.park(ITEM, "max-attempts")
        self.assertFalse(backend.publish(ITEM, SECOND))
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("last_refusal"),
                         "park-not-definite")
        for why in ("parked-body-already-refused", "park-not-definite",
                    "parked-cycle-ambiguous", "attempt-unclassified", "parked-history-saturated"):
            self.assertIn(why, DesignAClaimBackend.REFUSALS)

    def test_saturation_outlives_a_delivery_of_the_id(self):
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        outbox._write_item(backend.root, ITEM, {
            "item_id": ITEM, "status": "DELIVERED", "attempts": 1, "saturated": True,
            "payload": FIRST.decode(), "payload_digest": hashlib.sha256(FIRST).hexdigest(),
            "parked_digests": []})
        self.assertFalse(backend.publish(ITEM, SECOND), "a saturated id admits nothing new")


class RequeueKeepsTheParkedBody(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_one_explicit_retry_does_not_readmit_the_body_automatically(self):
        prov = _Provider(refusals=1)
        backend, core = _parked(self.tmp, prov)                       # A parks (definite)
        self.assertIs(outbox.requeue_item(backend.root, ITEM, operator="op"),
                      outbox.RequeueOutcome.REQUEUED)
        self.assertIn(hashlib.sha256(FIRST).hexdigest(),
                      outbox._read_item(backend.root, ITEM).get("parked_digests"),
                      "the requeue folds the parked body into the id's history")
        self.assertIs(core.deliver_one(ITEM, FIRST).outcome, DeliveryOutcome.CONFIRMED,
                      "the operator's one retry goes out")
        self.assertEqual(_status(backend), "DELIVERED")
        self.assertFalse(backend.publish(ITEM, FIRST),
                         "the same body is not readmitted by the delivered-republish path")
        self.assertTrue(backend.publish(ITEM, SECOND), "a never-parked body still is")

    def test_a_legacy_park_without_a_digest_is_folded_by_its_stored_text(self):
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        outbox._write_item(backend.root, ITEM, {"item_id": ITEM, "status": "PARKED",
                                                "attempts": 1, "reason": "permanent-refusal",
                                                "payload": FIRST.decode("utf-8")})
        self.assertIs(outbox.requeue_item(backend.root, ITEM, operator="op"),
                      outbox.RequeueOutcome.REQUEUED)
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("parked_digests"),
                         [hashlib.sha256(FIRST).hexdigest()])


class TheCapabilityIsReadNotDeclared(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_without_the_capability_a_definite_refusal_admits_nothing(self):
        prov = _Provider(refusals=1)
        backend, core = _parked(self.tmp, prov)
        self.assertEqual(outbox._read_item(backend.root, ITEM).get("reason"), "permanent-refusal")
        with mock.patch.object(DesignAClaimBackend, "capabilities",
                               BackendCapabilities(supports_force_release=True,
                                                   fresh_cycle_after_definite_park=False)):
            self.assertFalse(backend.publish(ITEM, SECOND),
                             "publish reads the flag; it is not a comment")
        self.assertTrue(backend.publish(ITEM, SECOND))


_LEGACY_MIXED_CYCLE = r"""
import sys, pathlib
sys.path.insert(0, sys.argv[1])
from ag2_sparrow.delivery_core import (DesignAClaimBackend, DeliveryCore, RetryPolicy,
                                       ProviderCapabilities, ProviderIndeterminate,
                                       ProviderPermanentRefused)
root, item, body = pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4].encode()
class Mixed:
    capabilities = ProviderCapabilities(idempotent_send=True)
    def __init__(self):
        self.n = 0
    def deliver(self, item_id, payload, key):
        self.n += 1
        if self.n == 1:
            raise ProviderIndeterminate("response lost after the broker stored A")
        raise ProviderPermanentRefused("declined")
    def reconcile(self, attempt):
        return None
backend = DesignAClaimBackend(root)
core = DeliveryCore(backend, Mixed(), policy=RetryPolicy(max_attempts=3, defer_idempotent_resend=True),
                    worker="gateway-result-drain")
assert backend.publish(item, body)
core.deliver_one(item, body)
core.deliver_one(item, body)
"""


class RecordsWithoutAttemptEvidenceFailClosed(unittest.TestCase):
    """Certainty is never inferred from silence. A record written before
    attempts were tracked (an upgrade in place) carries no `cycle_ambiguous`
    and no attempt counts; reading those absences as "clean" admitted a fresh
    cycle for a parked id whose body the broker may already hold. Only a record
    that proves every started attempt was classified admits one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _assert_refused(self, backend, why):
        self.assertFalse(backend.publish(ITEM, SECOND),
                         "a record that cannot prove its cycle definite admits nothing")
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual(rec.get("last_refusal"), why)
        self.assertEqual(rec.get("status"), "PARKED", "the id stays visible to the operator")
        core = DeliveryCore(backend, _Provider(refusals=0), policy=RetryPolicy(max_attempts=CAP),
                            worker="w1")
        self.assertIs(core.deliver_one(ITEM, SECOND).status, DrainStatus.TERMINAL,
                      "the gateway quarantines B instead of sending it")

    def test_a_record_from_before_attempts_were_tracked_refuses_a_fresh_cycle(self):
        """The on-disk shape the merge base leaves after an indeterminate call
        stored A and a permanent refusal parked it (reviewer's probe)."""
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        outbox._write_item(backend.root, ITEM, {
            "item_id": ITEM, "payload": FIRST.decode(), "payload_digest": None,
            "status": "PARKED", "reason": "permanent-refusal", "attempts": 2,
            "cycle_ambiguous": None, "published_at": 1.0})
        self._assert_refused(backend, "attempt-evidence-missing")

    def test_a_record_the_merge_base_wrote_refuses_a_fresh_cycle(self):
        """The same, generated by the merge base's own code: the delivery core
        at d37b79474 drives the mixed cycle, HEAD opens the unchanged outbox."""
        base = self.tmp / "base"
        base.mkdir()
        archive = subprocess.run(["git", "-C", str(_REPO), "archive", "--format=tar", "d37b79474",
                                  "packages/ag2-sparrow/ag2_sparrow"], capture_output=True)
        if archive.returncode != 0:
            self.skipTest("the merge base is not reachable from this checkout")
        subprocess.run(["tar", "-x", "-C", str(base)], input=archive.stdout, check=True)
        proc = subprocess.run([sys.executable, "-c", _LEGACY_MIXED_CYCLE,
                               str(base / "packages" / "ag2-sparrow"), str(self.tmp / ".outbox"),
                               ITEM, FIRST.decode()], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-600:])
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual((rec.get("status"), rec.get("reason")), ("PARKED", "permanent-refusal"))
        self.assertNotIn("attempts_started", rec, "the base tracked no attempts")
        self.assertFalse(rec.get("cycle_ambiguous"), "the base recorded no taint either")
        self._assert_refused(backend, "attempt-evidence-missing")

    def test_a_torn_count_refuses_a_fresh_cycle(self):
        """More attempts started than classified, with no pending mark left to
        explain it: the record is torn, not definite."""
        backend = DesignAClaimBackend(self.tmp / ".outbox")
        outbox._write_item(backend.root, ITEM, {
            "item_id": ITEM, "payload": FIRST.decode(),
            "payload_digest": hashlib.sha256(FIRST).hexdigest(),
            "status": "PARKED", "reason": "permanent-refusal", "attempts": 2,
            "attempts_started": 2, "attempts_classified": 1})
        self._assert_refused(backend, "attempt-evidence-missing")

    def test_a_cycle_this_code_drove_carries_its_own_proof(self):
        prov = _Provider(refusals=1)
        backend, core = _parked(self.tmp, prov)
        rec = outbox._read_item(backend.root, ITEM)
        self.assertEqual((rec.get("attempts_started"), rec.get("attempts_classified")), (1, 1))
        self.assertTrue(backend.publish(ITEM, SECOND), "proven definite: a fresh cycle")


if __name__ == "__main__":
    unittest.main()
