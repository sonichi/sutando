#!/usr/bin/env python3
"""A dispatched job that goes quiet has TWO causes, and one of them is not the consumer.

`DISPATCHED ... never consumed, so this is the consumer` is a claim about the task
bridge. It also fires when the consumer ran fine and the job simply published
nothing — a removed producer, or a `[no-send]` result by design. Measured
2026-09-05 on `daily-insight`: the task was consumed and a result archived, while
the probe reported it as never consumed and pointed at the wrong layer.

The fallback to task-cron results only runs when a job has NO dated artifacts, so
a job that published artifacts and then lost its producer keeps its history, the
fallback never fires, and its completion record stays invisible.
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
HISTORY = [(f"2026-08-0{i}", DUE + 1) for i in range(1, 8)]


def job(**over):
    j = {"name": "daily-insight", "hour": DUE_H, "minute": DUE_M,
         "artifacts": HISTORY, "today_seen": False, "minutes_since_due": 120,
         "conditional": False, "stem_declared": True, "dispatched_today": DUE}
    j.update(over)
    return j


class CompletedButUnpublished(unittest.TestCase):
    def test_completion_record_blames_the_producer_not_the_consumer(self):
        d = hc._interpret_daily_punctuality([job(completion_today=True)])
        self.assertEqual(d["status"], "warn")
        self.assertIn("COMPLETED", d["detail"])
        self.assertIn("the producer, not the consumer", d["detail"])
        self.assertNotIn("never consumed", d["detail"])

    def test_no_completion_record_still_blames_the_consumer(self):
        """The discriminating pair: same job, only completion_today differs."""
        d = hc._interpret_daily_punctuality([job(completion_today=False)])
        self.assertIn("never consumed", d["detail"])
        self.assertNotIn("the producer, not the consumer", d["detail"])

    def test_absent_key_keeps_the_old_verdict(self):
        """Back-compat: fixtures predating the field must not change meaning."""
        j = job()
        j.pop("completion_today", None)
        d = hc._interpret_daily_punctuality([j])
        self.assertIn("never consumed", d["detail"])

    def test_unpublished_alone_still_fails_the_green_gate(self):
        """A job in the new bucket must not let the probe report ok."""
        d = hc._interpret_daily_punctuality([job(completion_today=True)])
        self.assertNotEqual(d["status"], "ok")

    def test_a_healthy_job_is_still_ok(self):
        """Deliberately GREEN: without it, an always-warn probe reads as vigilance."""
        d = hc._interpret_daily_punctuality([job(today_seen=True, completion_today=True)])
        self.assertEqual(d["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
