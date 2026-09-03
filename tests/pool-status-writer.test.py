#!/usr/bin/env python3
"""PoolStatusWriter contract: snapshot content, atomic refresh, throttle."""
import json
import sys
import tempfile
import threading
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
        (self.tasks / "task-a.assigned-worker-1.txt").write_text("x")
        (self.tasks / "task-b.claimed-worker-1.txt").write_text("x")
        (self.tasks / "task-c.claimed-worker-2.txt").write_text("x")
        (self.tasks / "task-d.txt").write_text("x")  # unassigned: not in flight
        w = self.writer(["worker-1", "worker-2", "worker-3"], {"worker-1", "worker-2"})
        self.assertTrue(w.maybe_write())
        got = self.read()
        self.assertEqual(got["live_workers"], ["worker-1", "worker-2"])
        self.assertEqual(got["dead_workers"], ["worker-3"])
        # legacy keys ride to the broker for one release
        self.assertEqual(got["live_cores"], ["worker-1", "worker-2"])
        self.assertEqual(got["dead_cores"], ["worker-3"])
        self.assertEqual(got["in_flight"], {"worker-1": 2, "worker-2": 1})
        self.assertEqual(got["ts"], 1000)
        self.assertEqual(got["writer"], "pool-lead")

    def test_throttle_skips_within_window_and_refreshes_after(self):
        w = self.writer(["worker-1"], {"worker-1"})
        self.assertTrue(w.maybe_write())
        self.clock[0] += 10
        self.assertFalse(w.maybe_write())  # inside 30s window
        self.assertEqual(self.read()["ts"], 1000)
        self.clock[0] += 25
        self.assertTrue(w.maybe_write())  # window passed
        self.assertEqual(self.read()["ts"], 1035)

    def test_write_error_fails_open(self):
        w = self.writer(["worker-1"], {"worker-1"})
        self.state.rmdir()
        self.state.write_text("not a dir")  # mkdir/replace will fail
        self.assertFalse(w.maybe_write())

    def test_two_concurrent_writers_lose_nothing_and_tear_nothing(self):
        # Two briefly-overlapping leads must not contend for one temp path:
        # every attempted write lands and no reader sees a partial file.
        writes_each = 400
        writers = [
            PoolStatusWriter(self.tasks, self.state, lambda: ["worker-1"],
                             lambda i: True, refresh_s=0.0)
            for _ in range(2)]
        self.assertTrue(writers[0].maybe_write())  # file exists before readers
        swallowed = [0, 0]
        torn = [0]
        stop = threading.Event()
        start = threading.Barrier(3)

        def write(idx):
            start.wait()
            for _ in range(writes_each):
                if not writers[idx].maybe_write():
                    swallowed[idx] += 1

        def read():
            start.wait()
            while not stop.is_set():
                try:
                    self.read()
                except (OSError, ValueError):
                    torn[0] += 1

        threads = [threading.Thread(target=write, args=(0,)),
                   threading.Thread(target=write, args=(1,)),
                   threading.Thread(target=read)]
        for t in threads:
            t.start()
        for t in threads[:2]:
            t.join()
        stop.set()
        threads[2].join()
        self.assertEqual(sum(swallowed), 0, f"swallowed per writer: {swallowed}")
        self.assertEqual(torn[0], 0)
        self.assertEqual(self.read()["writer"], "pool-lead")
        self.assertEqual([f.name for f in self.state.iterdir()],
                         ["pool-status.json"])  # no temp files left behind


if __name__ == "__main__":
    unittest.main()
