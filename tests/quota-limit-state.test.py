#!/usr/bin/env python3
"""`classify_limit_state`: a core serving on extra usage (overage) is available,
not paused on a usage limit (issue #5283). Every row of the shared parity table
is asserted here and by tests/credential-proxy-limit-state.test.ts, so the
Python policy and the proxy's TypeScript copy cannot drift.

Run: python3 tests/quota-limit-state.test.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import quota_availability as qa  # noqa: E402

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "quota-limit-state.parity.json").read_text())
P = "anthropic-ratelimit-unified-"
PROXY = "http://localhost:7846"
INCIDENT = {P + "status": "rejected", P + "7d-status": "rejected", P + "7d-utilization": "1.0",
            P + "overage-status": "allowed"}


class ParityTable(unittest.TestCase):
    def test_every_row(self):
        for row in FIXTURE["limitState"]:
            with self.subTest(row["name"]):
                self.assertEqual(qa.classify_limit_state(row["record"]), row["expect"])


class Availability(unittest.TestCase):
    def _rec(self, **extra):
        d = {"available": False, "last_checked": "2026-10-09T12:00:00.000Z", "headers": dict(INCIDENT)}
        d.update(extra)
        return d

    def test_overage_is_available_even_against_a_false_proxy_flag(self):
        self.assertTrue(qa.resolve_available("rejected", False, INCIDENT, "overage"))
        d = qa.availability_decision(self._rec(), base_url=PROXY, stale=False)
        self.assertEqual((d["available"], d["limit_state"], d["unavailable_reason"]), (True, "overage", None))

    def test_a_newer_429_ends_overage(self):
        rec = self._rec(recent_rejections=[{"ts": "2026-10-09T12:00:09.000Z", "status": 429}])
        d = qa.availability_decision(rec, base_url=PROXY, stale=False)
        self.assertEqual((d["available"], d["limit_state"], d["unavailable_reason"]), (False, "rejected", "rejected"))

    def test_stale_and_unrouted_overage_still_fail_closed(self):
        self.assertEqual(qa.availability_decision(self._rec(), base_url=PROXY, stale=True)["unavailable_reason"], "stale")
        self.assertEqual(qa.availability_decision(self._rec(), base_url=None, stale=False)["unavailable_reason"], "not-routed")

    def test_non_overage_inputs_resolve_exactly_as_before(self):
        cases = [("rejected", True, None), ("allowed_warning", True, None), ("allowed", False, None),
                 ("allowed", None, None), ("allowed_warning", None, None),
                 ("allowed", True, {P + "7d-status": "rejected"}), ("allowed", True, {})]
        for st, flag, headers in cases:
            for state in (None, "allowed", "rejected"):
                with self.subTest(st=st, flag=flag, headers=headers, state=state):
                    self.assertEqual(qa.resolve_available(st, flag, headers, state),
                                     qa.resolve_available(st, flag, headers))

    def test_the_delivery_gate_stays_strict_on_overage(self):
        self.assertFalse(qa.gate_windows_allowed(self._rec()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
