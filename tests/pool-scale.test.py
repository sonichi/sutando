#!/usr/bin/env python3
"""pool_scale (L6): scale-up only on full saturation + backlog + cooldown,
scale-down only after the quiet window, and the ledger survives IO.

Run: python3 tests/pool-scale.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_scale import ScaleLedger, decide  # noqa: E402

T = 10_000.0


def call(**kw):
    base = dict(pending_unassigned=2, in_flight={"core-1": 3, "core-2": 3},
                current_n=2, min_n=1, max_n=4,
                last_change_ts=0.0, last_busy_ts=T, now=T)
    base.update(kw)
    return decide(**base)


class ScaleUpTests(unittest.TestCase):
    def test_saturated_with_backlog_scales_up(self):
        self.assertEqual(call(), 3)

    def test_one_idle_core_blocks_scale_up(self):
        self.assertIsNone(call(in_flight={"core-1": 3, "core-2": 1}))

    def test_no_backlog_blocks_scale_up(self):
        self.assertIsNone(call(pending_unassigned=0))

    def test_cap_blocks_scale_up(self):
        self.assertIsNone(call(current_n=4))

    def test_cooldown_blocks_scale_up(self):
        self.assertIsNone(call(last_change_ts=T - 100))

    def test_owner_example_every_core_over_three(self):
        self.assertEqual(
            call(in_flight={"core-1": 4, "core-2": 5}, pending_unassigned=1),
            3)


class ScaleDownTests(unittest.TestCase):
    def test_long_idle_scales_down(self):
        self.assertEqual(
            call(pending_unassigned=0, in_flight={"core-1": 0, "core-2": 0},
                 last_busy_ts=T - 2000, last_change_ts=T - 2000), 1)

    def test_recent_busy_blocks_scale_down(self):
        self.assertIsNone(
            call(pending_unassigned=0, in_flight={"core-1": 0, "core-2": 0},
                 last_busy_ts=T - 100, last_change_ts=T - 2000))

    def test_min_floor_blocks_scale_down(self):
        self.assertIsNone(
            call(pending_unassigned=0, in_flight={"core-1": 0},
                 current_n=1, last_busy_ts=T - 2000, last_change_ts=T - 2000))

    def test_total_follower_loss_does_not_shrink_the_pool(self):
        """`all([])` is True, so an empty in_flight — every follower gone —
        reads as a fully idle pool and shrinks it during the outage. The
        up-branch is guarded with `and live`; both must agree."""
        self.assertIsNone(
            call(pending_unassigned=0, in_flight={},
                 last_busy_ts=T - 2000, last_change_ts=T - 2000))

    def test_total_loss_with_backlog_also_holds(self):
        """Same empty pool, work waiting: neither branch may fire."""
        self.assertIsNone(
            call(pending_unassigned=5, in_flight={},
                 last_busy_ts=T - 2000, last_change_ts=T - 2000))

    def test_a_reporting_idle_pool_still_scales_down(self):
        """Control: the guard must reject only the EMPTY case, not idleness —
        otherwise scale-down is dead and the test above passes vacuously."""
        self.assertEqual(
            call(pending_unassigned=0, in_flight={"core-1": 0},
                 last_busy_ts=T - 2000, last_change_ts=T - 2000), 1)


class LedgerTests(unittest.TestCase):
    def test_roundtrip_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            led = ScaleLedger(d, now_fn=lambda: 42.0)
            self.assertEqual(led.load()["last_change_ts"], 0.0)
            led.record(changed=True, busy=True)
            got = led.load()
            self.assertEqual(got["last_change_ts"], 42.0)
            self.assertEqual(got["last_busy_ts"], 42.0)

    def test_corrupt_file_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            led = ScaleLedger(d)
            led.path.parent.mkdir(parents=True, exist_ok=True)
            led.path.write_text("not json")
            self.assertEqual(led.load()["last_busy_ts"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
