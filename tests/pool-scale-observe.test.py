#!/usr/bin/env python3
"""pool_scale observe/ledger IO branches: dir-scan counting, unreadable
tasks dir, the current_n guard, and the ledger's fail-open write.

Run: python3 tests/pool-scale-observe.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_scale import ScaleLedger, decide, observe  # noqa: E402


class ObserveTests(unittest.TestCase):
    def test_counts_pending_and_in_flight_per_follower(self):
        with tempfile.TemporaryDirectory() as td:
            tasks = Path(td)
            (tasks / "task-aaa.txt").write_text("x")
            (tasks / "task-bbb.txt").write_text("x")
            (tasks / "task-ccc.assigned-worker-1.txt").write_text("x")
            (tasks / "task-ddd.claimed-worker-2.txt").write_text("x")
            (tasks / "task-eee.claimed-worker-2.txt").write_text("x")
            (tasks / "notes.md").write_text("x")          # ignored
            (tasks / "task-cron-1.done").write_text("x")  # ignored: no .txt
            pending, in_flight = observe(tasks, ["worker-1", "worker-2"])
            self.assertEqual(pending, 2)
            self.assertEqual(in_flight, {"worker-1": 1, "worker-2": 2})

    def test_unknown_follower_suffix_is_not_counted(self):
        with tempfile.TemporaryDirectory() as td:
            tasks = Path(td)
            (tasks / "task-zzz.claimed-worker-9.txt").write_text("x")
            pending, in_flight = observe(tasks, ["worker-1"])
            self.assertEqual(pending, 0)
            self.assertEqual(in_flight, {"worker-1": 0})

    def test_unreadable_tasks_dir_reads_as_idle(self):
        pending, in_flight = observe(Path("/nonexistent-pool-observe"), ["worker-1"])
        self.assertEqual(pending, 0)
        self.assertEqual(in_flight, {"worker-1": 0})


class DecideGuardTests(unittest.TestCase):
    def test_zero_current_n_holds(self):
        self.assertIsNone(decide(
            pending_unassigned=5, in_flight={"worker-1": 9}, current_n=0,
            min_n=1, max_n=4, last_change_ts=0.0, last_busy_ts=0.0, now=1e4))


class LedgerIOTests(unittest.TestCase):
    def test_write_failure_is_fail_open(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            (state / "pool").write_text("a file where the dir should be")
            led = ScaleLedger(state, now_fn=lambda: 42.0)
            led.record(changed=True, busy=True)  # must not raise
            self.assertEqual(led.load(),
                             {"last_change_ts": 0.0, "last_busy_ts": 0.0})

    def test_corrupt_ledger_reads_as_zeros(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            (state / "pool").mkdir()
            (state / "pool" / "scale-ledger.json").write_text("{not json")
            self.assertEqual(ScaleLedger(state).load(),
                             {"last_change_ts": 0.0, "last_busy_ts": 0.0})


if __name__ == "__main__":
    unittest.main(verbosity=1)
