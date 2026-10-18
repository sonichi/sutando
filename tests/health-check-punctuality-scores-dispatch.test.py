#!/usr/bin/env python3
"""Punctuality is a property of the schedule, so it must be measured on the schedule.

Artifact mtimes date when work FINISHED. Scoring those and printing the result as
lateness reports pickup-and-execution latency as a broken cron — permanently, on a
host where every job dispatches within a minute of its cron time.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
sys.modules["hc"] = hc
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass

DUE_H, DUE_M = 6, 50
DUE = DUE_H * 60 + DUE_M
DATES = [f"2026-08-{d:02d}" for d in range(24, 31)]

# What the host actually shows: dispatched on the minute, output ~30 min later.
ON_TIME_DISPATCH = [(d, DUE + 1) for d in DATES]
LATE_OUTPUT = [(d, DUE + 30) for d in DATES]


def job(**over):
    j = {"name": "morning-briefing", "hour": DUE_H, "minute": DUE_M,
         "artifacts": LATE_OUTPUT, "today_seen": True, "minutes_since_due": 0,
         "conditional": False, "stem_declared": True, "dispatched_today": DUE + 1,
         "dispatch_history": ON_TIME_DISPATCH}
    j.update(over)
    return j


class TestScoreTheScheduleNotTheOutput(unittest.TestCase):
    def test_on_time_dispatch_is_not_a_late_schedule(self):
        r = hc._interpret_daily_punctuality([job()])
        self.assertEqual(r["status"], "ok", r)
        self.assertNotIn("min late", r["detail"])

    def test_the_latency_is_still_reported_just_not_as_lateness(self):
        r = hc._interpret_daily_punctuality([job()])
        self.assertIn("trails by median +30 min", r["detail"])
        self.assertIn("not the schedule", r["detail"])

    def test_a_genuinely_late_schedule_still_warns(self):
        r = hc._interpret_daily_punctuality(
            [job(dispatch_history=[(d, DUE + 45) for d in DATES])])
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("median +45 min late", r["detail"])

    def test_without_dispatch_evidence_output_is_still_scored(self):
        r = hc._interpret_daily_punctuality([job(dispatch_history=[])])
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("median +30 min late", r["detail"])

    def test_the_warn_no_longer_asserts_what_it_did_not_measure(self):
        r = hc._interpret_daily_punctuality([job(dispatch_history=[])])
        self.assertNotIn("something else is covering for it", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
