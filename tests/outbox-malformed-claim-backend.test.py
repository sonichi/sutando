#!/usr/bin/env python3
"""A malformed claim through DesignAClaimBackend: never taken on the torn-write
grace, and still retired from a terminal item once its owner is gone past the TTL.

Run: python3 tests/outbox-malformed-claim-backend.test.py
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
from unittest.mock import patch
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow import outbox  # noqa: E402
from ag2_sparrow.delivery_core import (  # noqa: E402
    DeliveryCore, DeliveryOutcome, DeliveryReceipt, DesignAClaimBackend, DrainStatus,
    ProviderCapabilities)

_dead = subprocess.Popen([sys.executable, "-c", "pass"])
_dead.wait()
DEAD_PID = _dead.pid
ITEM = "room-evt-malformed"
TTL = 300.0
# (label, raw JSON for the pid, plausibly names a process)
PID_FORMS = [
    ("int", lambda pid: str(pid), True),
    ("digit string", lambda pid: f'"{pid}"', True),
    ("integral float", lambda pid: f"{pid}.0", True),
    ("leading-zero string", lambda pid: f'"0{pid}"', True),
    ("plus-sign string", lambda pid: f'"+{pid}"', True),
    ("padded string", lambda pid: f'" {pid} "', True),
    ("underscore string", lambda pid: f'"{str(pid)[:1]}_{str(pid)[1:]}"', True),
    ("fractional float", lambda pid: f"{pid}.5", True),
    ("negative string", lambda pid: '"-1"', False),
    ("word string", lambda pid: '"abc"', False),
    ("true", lambda pid: "true", True),
    ("false", lambda pid: "false", False),
    ("oversized", lambda pid: "9" * 5001, False),
    ("2**31", lambda pid: str(2**31), False),
]


@contextlib.contextmanager
def owner_stand_in(module, states):
    """Pids a test cannot own (1, PID_MAX) get a patched liveness probe: {pid: alive}."""
    real = module.process_identity

    def probe(pid):
        if pid in states:
            return module.ProcessIdentity(pid, module.OwnerState.ALIVE if states[pid]
                                          else module.OwnerState.DEAD)
        return real(pid)
    with patch.object(module, "process_identity", probe):
        yield


def pid1_stand_in(module, alive):
    return owner_stand_in(module, {1: alive})


class MalformedClaimBackend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _rewrite(self, backend, patch=None, text=None, age=0.0):
        p = outbox._claim_path(self.root, ITEM)
        if text is None:
            rec = json.loads(p.read_text(encoding="utf-8"))
            rec.update(patch)
            text = json.dumps(rec)
        p.write_text(text, encoding="utf-8")
        t = time.time() - age
        os.utime(p, (t, t))

    def test_live_owner_bad_token_not_taken_after_grace(self):
        b = DesignAClaimBackend(self.root, reclaim_ttl_s=1e9)
        self.assertTrue(b.publish(ITEM, b"hi"))
        self.assertIsNotNone(b.claim(ITEM, "A"))
        self._rewrite(b, {"start_usec": -1}, age=outbox.SWAP_GRACE_S + 1)
        self.assertIsNone(b.claim(ITEM, "B"), "a live owner's malformed claim was taken")
        self.assertEqual(outbox.read_delivery_claim(self.root, ITEM).drainer_id, "A")

    def test_torn_claim_still_taken_after_grace(self):
        b = DesignAClaimBackend(self.root, reclaim_ttl_s=1e9)
        self.assertTrue(b.publish(ITEM, b"hi"))
        self.assertIsNotNone(b.claim(ITEM, "A"))
        self._rewrite(b, text='{"item_id": "room-evt-mal', age=outbox.SWAP_GRACE_S + 1)
        self.assertIsNotNone(b.claim(ITEM, "B"), "a torn claim must still age out")

    def test_terminal_claim_with_oversize_int_keeps_identity_and_retires(self):
        b = DesignAClaimBackend(self.root, reclaim_ttl_s=TTL)
        self.assertTrue(b.publish(ITEM, b"hi"))
        self.assertIsNotNone(b.claim(ITEM, "A"))
        outbox.record_delivered(self.root, ITEM)
        p = outbox._claim_path(self.root, ITEM)
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec.update(pid=DEAD_PID, start_usec=123456789, claimed_at="@BIG@", extra="@BIG@")
        p.write_text(json.dumps(rec).replace('"@BIG@"', "9" * 5001), encoding="utf-8")
        t = time.time() - TTL
        os.utime(p, (t, t))
        got = outbox._read_claim_at(p, "")
        self.assertEqual((got.state, got.malformed, got.item_id, got.drainer_id, got.pid,
                          got.start_usec), ("UNKNOWN", True, ITEM, "A", DEAD_PID, 123456789))
        self.assertEqual(b.recover().retired, [ITEM])
        self.assertIsNone(outbox.read_delivery_claim(self.root, ITEM))

    def _oversize_claimed_at(self, age):
        """The live owner's own claim, valid pid and birth token, claimed_at oversized."""
        p = outbox._claim_path(self.root, ITEM)
        text = p.read_text(encoding="utf-8")
        rec = json.loads(text, parse_int=lambda s: s if len(s) > 20 else int(s))
        rec["claimed_at"] = "@BIG@"
        p.write_text(json.dumps(rec).replace('"@BIG@"', "9" * 5001), encoding="utf-8")
        t = time.time() - age
        os.utime(p, (t, t))

    def test_live_owner_oversize_claimed_at_never_reclaimed(self):
        self.assertTrue(outbox.acquire_delivery_claim(self.root, ITEM, "A"))
        for age in (TTL + 1, TTL * 100):
            self._oversize_claimed_at(age)
            self.assertFalse(outbox.reclaim_delivery_claim(self.root, ITEM, TTL, "B"),
                             f"a live owner's claim was reclaimed at {age}s")
            self.assertEqual(outbox.read_delivery_claim(self.root, ITEM).drainer_id, "A")

    def test_live_owner_oversize_claimed_at_one_provider_call(self):
        gate, entered, calls = threading.Event(), threading.Event(), []

        class Blocking:
            capabilities = ProviderCapabilities()

            def __init__(self, block=True):
                self.block = block

            def deliver(self, item_id, payload, key):
                calls.append(item_id)
                if self.block:
                    entered.set()
                    gate.wait(10)
                return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED)

            def reconcile(self, attempt):
                return None

        backend = DesignAClaimBackend(self.root, reclaim_ttl_s=TTL)
        self.assertTrue(backend.publish(ITEM, b"hi"))
        a = threading.Thread(target=DeliveryCore(backend, Blocking(), worker="A").deliver_one,
                             args=(ITEM, b"hi"))
        a.start()
        try:
            self.assertTrue(entered.wait(10), "drainer A never reached the provider")
            results = []
            for age in (TTL + 1, TTL * 100):
                if outbox.read_delivery_claim(self.root, ITEM) is None:
                    break                       # B already took and released it
                self._oversize_claimed_at(age)
                results.append(DeliveryCore(DesignAClaimBackend(self.root, reclaim_ttl_s=TTL),
                                            Blocking(block=False), worker="B")
                               .deliver_one(ITEM, b"hi").status)
        finally:
            gate.set()
            a.join(10)
        self.assertEqual(len(calls), 1, f"provider called {len(calls)} times")
        self.assertEqual(results, [DrainStatus.NOT_CLAIMED] * 2)

    def _rewrite_pid(self, raw_pid, age):
        p = outbox._claim_path(self.root, ITEM)
        rec = json.loads(p.read_text(encoding="utf-8"),
                         parse_int=lambda s: s if len(s) > 20 else int(s))
        rec.update(pid="@PID@", claimed_at="not-a-time")
        p.write_text(json.dumps(rec).replace('"@PID@"', raw_pid), encoding="utf-8")
        t = time.time() - age
        os.utime(p, (t, t))

    def _drain_while_a_sends(self, raw_pid, age):
        """A blocks inside the provider; B drains the rewritten claim. Provider calls."""
        gate, entered, calls = threading.Event(), threading.Event(), []

        class Provider:
            capabilities = ProviderCapabilities()

            def __init__(self, block):
                self.block = block

            def deliver(self, item_id, payload, key):
                calls.append(item_id)
                if self.block:
                    entered.set()
                    gate.wait(10)
                return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED)

            def reconcile(self, attempt):
                return None

        backend = DesignAClaimBackend(self.root, reclaim_ttl_s=TTL)
        self.assertTrue(backend.publish(ITEM, b"hi"))
        a = threading.Thread(target=DeliveryCore(backend, Provider(True), worker="A")
                             .deliver_one, args=(ITEM, b"hi"))
        a.start()
        try:
            self.assertTrue(entered.wait(10), "drainer A never reached the provider")
            self._rewrite_pid(raw_pid, age)
            DeliveryCore(DesignAClaimBackend(self.root, reclaim_ttl_s=TTL), Provider(False),
                         worker="B").deliver_one(ITEM, b"hi")
        finally:
            gate.set()
            a.join(10)
        return len(calls)

    def test_pid_forms_block_or_release_by_plausibility(self):
        for label, render, plausible in PID_FORMS:
            for alive, pid in ((True, os.getpid()), (False, DEAD_PID)):
                held = plausible and alive
                with self.subTest(form=label, alive=alive), pid1_stand_in(outbox, alive):
                    self.tearDown()
                    self.setUp()
                    self.assertEqual(self._drain_while_a_sends(render(pid), TTL + 1),
                                     1 if held else 2)
                    self.tearDown()
                    self.setUp()
                    b = DesignAClaimBackend(self.root, reclaim_ttl_s=TTL)
                    self.assertTrue(b.publish(ITEM, b"hi"))
                    self.assertIsNotNone(b.claim(ITEM, "A"))
                    outbox.record_delivered(self.root, ITEM)
                    self._rewrite_pid(render(pid), TTL - 1)
                    self.assertEqual(b.recover().retired, [])
                    self._rewrite_pid(render(pid), TTL)
                    self.assertEqual(b.recover().retired, [] if held else [ITEM])

    def test_owner_identity_axis(self):
        """Vary who owns the claim, not only how its pid is written."""
        pid_max = 2**31 - 1
        owners = [("own pid", os.getpid(), None, True), ("dead pid", DEAD_PID, None, False)]
        for name, pid in (("pid 1", 1), ("PID_MAX", pid_max)):
            owners += [(name, pid, True, True), (name, pid, False, False)]
        forms = [f for f in PID_FORMS if f[0] in (
            "int", "digit string", "integral float", "leading-zero string", "padded string")]
        for name, pid, patched, alive in owners:
            states = {} if patched is None else {pid: patched}
            for label, render, _ in forms:
                with self.subTest(owner=name, alive=alive, form=label), \
                        owner_stand_in(outbox, states):
                    self.tearDown()
                    self.setUp()
                    self.assertEqual(self._drain_while_a_sends(render(pid), TTL + 1),
                                     1 if alive else 2)
                    self.tearDown()
                    self.setUp()
                    b = DesignAClaimBackend(self.root, reclaim_ttl_s=TTL)
                    self.assertTrue(b.publish(ITEM, b"hi"))
                    self.assertIsNotNone(b.claim(ITEM, "A"))
                    outbox.record_delivered(self.root, ITEM)
                    self._rewrite_pid(render(pid), TTL)
                    self.assertEqual(b.recover().retired, [] if alive else [ITEM])

    def test_padded_live_pid_blocks_at_every_length(self):
        pid = str(os.getpid())
        for length in (1, 10, 32, 33, 64, 5000):
            width = max(length, len(pid))
            for kind, text in (("zero", pid.zfill(width)),
                               ("space", pid.rjust((width + len(pid)) // 2).ljust(width))):
                raw = json.dumps(text)
                with self.subTest(kind=kind, length=len(text)):
                    self.tearDown()
                    self.setUp()
                    self.assertEqual(self._drain_while_a_sends(raw, TTL + 1), 1)
                    self.tearDown()
                    self.setUp()
                    b = DesignAClaimBackend(self.root, reclaim_ttl_s=TTL)
                    self.assertTrue(b.publish(ITEM, b"hi"))
                    self.assertIsNotNone(b.claim(ITEM, "A"))
                    outbox.record_delivered(self.root, ITEM)
                    self._rewrite_pid(raw, TTL * 100)
                    self.assertEqual(b.recover().retired, [])

    def test_unicode_zero_padded_live_pid_blocks(self):
        pid = str(os.getpid())
        zeros = [chr(cp) for cp in range(sys.maxunicode + 1)
                 if unicodedata.decimal(chr(cp), None) == 0]
        for length in (33, 64):
            for kind, pad in (("U+0660", "\u0660" * length),
                              ("mixed-script", "".join(zeros[i % len(zeros)]
                                                       for i in range(length)))):
                raw = json.dumps(pad[:length - len(pid)] + pid)
                with self.subTest(kind=kind, length=length):
                    self.tearDown()
                    self.setUp()
                    self.assertEqual(self._drain_while_a_sends(raw, TTL + 1), 1)
                    self.tearDown()
                    self.setUp()
                    b = DesignAClaimBackend(self.root, reclaim_ttl_s=TTL)
                    self.assertTrue(b.publish(ITEM, b"hi"))
                    self.assertIsNotNone(b.claim(ITEM, "A"))
                    outbox.record_delivered(self.root, ITEM)
                    self._rewrite_pid(raw, TTL * 100)
                    self.assertEqual(b.recover().retired, [])

    def _terminal_malformed(self, age):
        b = DesignAClaimBackend(self.root, reclaim_ttl_s=TTL)
        self.assertTrue(b.publish(ITEM, b"hi"))
        self.assertIsNotNone(b.claim(ITEM, "A"))
        outbox.record_delivered(self.root, ITEM)
        self._rewrite(b, {"pid": DEAD_PID, "start_usec": -1}, age=age)
        return b

    def test_terminal_malformed_claim_retired_at_ttl(self):
        b = self._terminal_malformed(TTL)
        self.assertEqual(b.recover().retired, [ITEM])
        self.assertIsNone(outbox.read_delivery_claim(self.root, ITEM))
        self.assertTrue(b.publish(ITEM, b"again"))

    def test_terminal_malformed_claim_kept_before_ttl(self):
        b = self._terminal_malformed(TTL - 1)
        self.assertEqual(b.recover().retired, [])
        self.assertIsNotNone(outbox.read_delivery_claim(self.root, ITEM))


if __name__ == "__main__":
    unittest.main()
