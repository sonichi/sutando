#!/usr/bin/env python3
"""A silent daily job has two causes, and blaming the schedule for both hides one.

`no output today, N min past due` reads as a schedule failure. It also fires when
the cron dispatched on time and nothing consumed the task — a core outage, which
is a different incident with a different fix.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
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

import cron_task_id  # noqa: E402

DUE_H, DUE_M = 6, 50
DUE = DUE_H * 60 + DUE_M
HISTORY = [(f"2026-08-0{i}", DUE + 1) for i in range(1, 8)]


def job(**over):
    j = {"name": "morning-briefing", "hour": DUE_H, "minute": DUE_M,
         "artifacts": HISTORY, "today_seen": False, "minutes_since_due": 120,
         "conditional": False, "stem_declared": True, "dispatched_today": None}
    j.update(over)
    return j


class TestDispatchSeparatesTheTwoCauses(unittest.TestCase):
    def test_no_dispatch_blames_the_schedule(self):
        r = hc._interpret_daily_punctuality([job()])
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("the schedule itself did not fire", r["detail"])
        self.assertNotIn("DISPATCHED", r["detail"])

    def test_dispatched_but_unconsumed_blames_the_consumer(self):
        r = hc._interpret_daily_punctuality([job(dispatched_today=DUE + 1)])
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("DISPATCHED 06:51", r["detail"])
        self.assertIn("never consumed", r["detail"])
        self.assertNotIn("the schedule itself did not fire", r["detail"])

    def test_the_two_verdicts_are_not_the_same_sentence(self):
        a = hc._interpret_daily_punctuality([job()])["detail"]
        b = hc._interpret_daily_punctuality([job(dispatched_today=DUE + 1)])["detail"]
        self.assertNotEqual(a, b)

    def test_a_healthy_job_is_unaffected(self):
        r = hc._interpret_daily_punctuality([job(today_seen=True, minutes_since_due=0)])
        self.assertEqual(r["status"], "ok", r)


class TestDispatchLaneReadsEmitTime(unittest.TestCase):
    def _write(self, root: Path, name: str, when: datetime) -> None:
        stamp = int(when.timestamp() * 1000)
        f = root / f"{cron_task_id.task_id(name, stamp)}.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"id: {cron_task_id.task_id(name, stamp)}\nsource: cron\n")

    def test_reads_the_name_epoch_not_the_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            tasks = Path(d)
            when = datetime(2026, 8, 30, 6, 51)
            self._write(tasks / "archive" / "2026-08", "morning-briefing", when)
            got = hc._daily_dispatch_minutes(tasks, "morning-briefing")
        self.assertEqual(got, [("2026-08-30", DUE + 1)])

    def test_a_neighbouring_job_does_not_vouch(self):
        with tempfile.TemporaryDirectory() as d:
            tasks = Path(d)
            self._write(tasks, "morning-briefing-extra", datetime(2026, 8, 30, 6, 51))
            got = hc._daily_dispatch_minutes(tasks, "morning-briefing")
        self.assertEqual(got, [])

    def test_an_unusable_stamp_is_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            tasks = Path(d)
            good = datetime(2026, 8, 30, 6, 51)
            self._write(tasks, "morning-briefing", good)
            # A stamp the matcher accepts but no clock can represent: the lane
            # must drop that one file, not abandon the whole scan.
            bad = tasks / f"{cron_task_id.TASK_PREFIX}morning-briefing-{'9' * 19}.txt"
            bad.write_text("id: x\n")
            got = hc._daily_dispatch_minutes(tasks, "morning-briefing")
        self.assertEqual(got, [("2026-08-30", DUE + 1)])

    def test_missing_tasks_dir_is_empty_not_an_error(self):
        self.assertEqual(hc._daily_dispatch_minutes(Path("/nonexistent-xyz"), "a"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
