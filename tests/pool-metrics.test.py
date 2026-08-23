#!/usr/bin/env python3
"""PoolMetrics (L4): append-only recording, fail-open on IO error, and a
summary that actually computes the quality-bar quantities.

Run: python3 tests/pool-metrics.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_metrics import PoolMetrics  # noqa: E402


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = PoolMetrics(self.tmp.name, now_fn=lambda: 1_700_000_000.0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_computes_quality_bar_quantities(self):
        self.m.assigned("task-1", "a", "C1", wait_s=1.0)
        self.m.assigned("task-2", "a", "C1", wait_s=3.0)
        self.m.assigned("task-3", "b", None, wait_s=200.0)  # head-of-line
        self.m.claimed("task-4", "b", fallback=True)
        self.m.reclaimed("task-5", "a")
        s = self.m.summarize()
        self.assertEqual(s["assignment_distribution"], {"a": 2, "b": 1})
        self.assertEqual(s["head_of_line_incidents"], 1)
        self.assertEqual(s["fallback_claims"], 1)
        self.assertEqual(s["reclaims"], 1)
        self.assertEqual(s["mean_wait_by_channel"], {"C1": 2.0})

    def test_corrupt_line_counted_not_fatal(self):
        self.m.assigned("task-1", "a", None, wait_s=0.5)
        with open(self.m._path(), "a") as f:
            f.write("{not json\n")
        self.m.assigned("task-2", "b", None, wait_s=0.5)
        s = self.m.summarize()
        self.assertEqual(s["rows"], 2)
        self.assertEqual(s["bad_lines"], 1)

    def test_record_is_fail_open_on_unwritable_dir(self):
        bad = PoolMetrics("/dev/null/nope", now_fn=lambda: 0.0)
        bad.assigned("task-1", "a", None, wait_s=0.1)  # must not raise
        self.assertIn("note", bad.summarize())

    def test_missing_day_reports_note_not_crash(self):
        self.assertIn("note", self.m.summarize(day="1999-01-01"))


    def test_continuity_breaks_count_same_channel_core_switches(self):
        # asymmetric (2 switches, 1 same-core pair) so an inverted
        # comparison cannot yield the same counts
        self.m.assigned("t1", "a", "C1", wait_s=0.0)
        self.m.assigned("t2", "b", "C1", wait_s=0.0)
        self.m.assigned("t3", "a", "C1", wait_s=0.0)
        self.m.assigned("t4", "a", "C1", wait_s=0.0)
        s = self.m.summarize()
        self.assertEqual(s["continuity_breaks"], 2)
        self.assertEqual(s["continuity_pairs"], 3)
        self.assertEqual(s["continuity_breaks_by_channel"], {"C1": 2})

    def test_switch_outside_window_is_not_a_break(self):
        self.m.assigned("t1", "a", "C1", wait_s=0.0)
        far = PoolMetrics(self.tmp.name, now_fn=lambda: 1_700_000_000.0 + 4000)
        far.assigned("t2", "b", "C1", wait_s=0.0)
        s = self.m.summarize()
        self.assertEqual(s["continuity_breaks"], 0)
        self.assertEqual(s["continuity_pairs"], 0)

if __name__ == "__main__":
    unittest.main(verbosity=2)
