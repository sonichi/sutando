#!/usr/bin/env python3
"""A drained item that has reached its own terminal state must not be reported
as contention.

`claim()` returns None for four different reasons — absent, terminal, held by
a live worker, incarnation vanished — and the core used to collapse all four
into NOT_CLAIMED, whose contract said "another worker owns it". A caller that
reads NOT_CLAIMED as transient then retries a parked item on every pass, which
is unbounded and, worse, silent: the log says "will retry" about a delivery
that has already been abandoned.

The ordering matters and is what makes this hard to test by accident: the item
parks CORRECTLY at MAX_ATTEMPTS, and the loop begins on the pass AFTER that.
A test that only asserts "reaches PARKED within MAX_ATTEMPTS" passes on the
broken code, because the defect starts once that assertion is already
satisfied. Every case below therefore drains at least once MORE than the cap.

Run: python3 tests/delivery-core-terminal-not-contended.test.py
"""
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
    DeliveryCore, DesignAClaimBackend, DesignCClaimBackend, ProviderRefused,
    RetryPolicy)
from ag2_sparrow.delivery_core.contract import (  # noqa: E402
    DrainStatus, ProviderCapabilities)

ITEM = "task-stuck-1"
PAYLOAD = b'{"id": "task-stuck-1", "body": "hi"}'
CAP = 5


class _AlwaysRefuses:
    """Provider that never delivers — drives the item to the attempt cap."""
    capabilities = ProviderCapabilities()

    def __init__(self):
        self.calls = 0

    def deliver(self, item_id, payload, idempotency_key):
        self.calls += 1
        raise ProviderRefused("destination is gone")

    def reconcile(self, attempt):
        return None


def _core(backend, provider):
    return DeliveryCore(backend, provider,
                        policy=RetryPolicy(max_attempts=CAP),
                        worker="test-drain")


def _drain_to_cap(core, provider):
    """Exhaust the budget exactly as production does, and prove we got there
    by real provider calls rather than by assuming the loop count."""
    for _ in range(CAP):
        core.deliver_one(ITEM, PAYLOAD)
    assert provider.calls == CAP, f"expected {CAP} provider calls, got {provider.calls}"


class TerminalIsNotContention(unittest.TestCase):

    def _backend_a(self, td):
        b = DesignAClaimBackend(Path(td) / "outbox")
        b.publish(ITEM, PAYLOAD)
        return b

    def test_drain_after_parking_reports_TERMINAL_not_NOT_CLAIMED(self):
        """The regression. This is the pass the old code looped on forever."""
        with tempfile.TemporaryDirectory() as td:
            b, p = self._backend_a(td), _AlwaysRefuses()
            core = _core(b, p)
            _drain_to_cap(core, p)
            self.assertTrue(b.is_terminal(ITEM),
                            "precondition: the item should be parked at the cap")

            res = core.deliver_one(ITEM, PAYLOAD)   # the pass AFTER parking

            self.assertIs(res.status, DrainStatus.TERMINAL)
            self.assertIsNot(res.status, DrainStatus.NOT_CLAIMED)
            self.assertIsNone(res.outcome, "no provider call was made")
            self.assertEqual(p.calls, CAP,
                             "a terminal item must not reach the provider again")

    def test_terminal_is_stable_across_further_passes(self):
        """Unbounded-loop guard: repeated drains stay TERMINAL and stay silent
        toward the provider, so a caller can stop on the first one."""
        with tempfile.TemporaryDirectory() as td:
            b, p = self._backend_a(td), _AlwaysRefuses()
            core = _core(b, p)
            _drain_to_cap(core, p)
            for i in range(4):
                self.assertIs(core.deliver_one(ITEM, PAYLOAD).status,
                              DrainStatus.TERMINAL, f"pass {i} after parking")
            self.assertEqual(p.calls, CAP)

    def test_live_contention_still_reports_NOT_CLAIMED(self):
        """The control. Terminal must not swallow the transient case it was
        split from, or this trades an infinite retry for a dropped delivery."""
        with tempfile.TemporaryDirectory() as td:
            b = self._backend_a(td)
            held = b.claim(ITEM, "someone-else")
            self.assertIsNotNone(held, "precondition: the claim should be taken")
            self.assertFalse(b.is_terminal(ITEM),
                             "a claimed-but-live item is not terminal")

            res = _core(b, _AlwaysRefuses()).deliver_one(ITEM, PAYLOAD)

            self.assertIs(res.status, DrainStatus.NOT_CLAIMED)

    def test_absent_item_is_not_terminal(self):
        """None-from-claim also covers 'never published'. That is not a
        decided item, so it must not be quarantined as one."""
        with tempfile.TemporaryDirectory() as td:
            b = DesignAClaimBackend(Path(td) / "outbox")
            self.assertFalse(b.is_terminal("never-published"))
            self.assertIs(
                _core(b, _AlwaysRefuses()).deliver_one("never-published", PAYLOAD).status,
                DrainStatus.NOT_CLAIMED)

    def test_backend_c_reports_terminal_by_location(self):
        """C records terminality as a directory move, not a status field, so
        the parity has to be asserted rather than assumed from A."""
        with tempfile.TemporaryDirectory() as td:
            b = DesignCClaimBackend(Path(td) / "outbox-c", activate=True)
            b.publish(ITEM, PAYLOAD)
            p = _AlwaysRefuses()
            core = _core(b, p)
            _drain_to_cap(core, p)
            self.assertTrue(b.is_terminal(ITEM))
            self.assertIs(core.deliver_one(ITEM, PAYLOAD).status,
                          DrainStatus.TERMINAL)
            self.assertFalse(b.is_terminal("some-other-item"),
                             "prefix match must not fire on an unrelated key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
