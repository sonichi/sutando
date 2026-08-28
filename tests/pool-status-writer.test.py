#!/usr/bin/env python3
"""PoolStatusWriter contract: snapshot content, atomic refresh, throttle."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "runtime-api"))
from pool_status import PoolStatusWriter  # noqa: E402


class PoolStatusWriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        self.tasks.mkdir()
        self.state.mkdir()
        self.clock = [1000.0]

    def tearDown(self):
        self.tmp.cleanup()

    def writer(self, followers, alive, refresh_s=30.0):
        return PoolStatusWriter(
            self.tasks, self.state, lambda: list(followers),
            lambda i: i in alive, now_fn=lambda: self.clock[0],
            refresh_s=refresh_s)

    def read(self):
        return json.loads((self.state / "pool-status.json").read_text())

    def test_snapshot_splits_live_dead_and_counts_in_flight(self):
        (self.tasks / "task-a.assigned-core-1.txt").write_text("x")
        (self.tasks / "task-b.claimed-core-1.txt").write_text("x")
        (self.tasks / "task-c.claimed-core-2.txt").write_text("x")
        (self.tasks / "task-d.txt").write_text("x")  # unassigned: not in flight
        w = self.writer(["core-1", "core-2", "core-3"], {"core-1", "core-2"})
        self.assertTrue(w.maybe_write())
        got = self.read()
        self.assertEqual(got["live_cores"], ["core-1", "core-2"])
        self.assertEqual(got["dead_cores"], ["core-3"])
        self.assertEqual(got["in_flight"], {"core-1": 2, "core-2": 1})
        self.assertEqual(got["ts"], 1000)
        self.assertEqual(got["writer"], "pool-lead")

    def test_throttle_skips_within_window_and_refreshes_after(self):
        w = self.writer(["core-1"], {"core-1"})
        self.assertTrue(w.maybe_write())
        self.clock[0] += 10
        self.assertFalse(w.maybe_write())  # inside 30s window
        self.assertEqual(self.read()["ts"], 1000)
        self.clock[0] += 25
        self.assertTrue(w.maybe_write())  # window passed
        self.assertEqual(self.read()["ts"], 1035)

    def test_write_error_fails_open(self):
        w = self.writer(["core-1"], {"core-1"})
        self.state.rmdir()
        self.state.write_text("not a dir")  # mkdir/replace will fail
        self.assertFalse(w.maybe_write())


class PoolStatusDegradedTest(PoolStatusWriterTest):
    def test_an_unreadable_tasks_dir_reports_no_in_flight_work(self):
        # The writer runs on a timer beside a directory other processes rename
        # under it. A raising snapshot would take the status file with it.
        (self.tasks / "task-a.assigned-core-1.txt").write_text("x")
        self.tasks.chmod(0o000)
        try:
            self.assertTrue(self.writer(["core-1"], {"core-1"}).maybe_write())
            self.assertEqual(self.read()["in_flight"], {})
        finally:
            self.tasks.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
