#!/usr/bin/env python3
"""activity_bus.in_progress: is the core on this task right now?

The Stop hook skips a task whose snapshot says RUNNING with fresh activity.
Run: python3 tests/activity-bus-in-progress.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import activity_bus as ab  # noqa: E402


class InProgress(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "state" / "activity").mkdir(parents=True)
        self.now = 1_800_000_000.0

    def tearDown(self):
        self.td.cleanup()

    def snap(self, task="task-a", **fields):
        (self.ws / "state" / "activity" / f"{task}.json").write_text(json.dumps({"task_id": task, **fields}))

    def test_running_with_fresh_activity_is_in_progress(self):
        self.snap(phase="RUNNING", started_at=self.now - 3000, last_activity_at=self.now - 60)
        self.assertTrue(ab.in_progress(self.ws, "task-a", now=self.now))

    def test_the_activity_stamp_wins_over_the_start(self):
        # A task queued long before pickup: started_at is the queue time, the activity is fresh.
        self.snap(phase="RUNNING", started_at=self.now - 5000, last_activity_at=self.now - 10)
        self.assertTrue(ab.in_progress(self.ws, "task-a", now=self.now))
        self.snap(phase="RUNNING", started_at=self.now - 10, last_activity_at=self.now - 5000)
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now))

    def test_started_at_serves_when_there_is_no_activity_stamp(self):
        self.snap(phase="RUNNING", started_at=self.now - 100)
        self.assertTrue(ab.in_progress(self.ws, "task-a", now=self.now))

    def test_stale_running_is_not(self):
        self.snap(phase="RUNNING", started_at=self.now - 4000, last_activity_at=self.now - 4000)
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now))
        self.assertTrue(ab.in_progress(self.ws, "task-a", max_age=5000, now=self.now))

    def test_other_phases_are_not(self):
        for phase in ("RECEIVED", "QUEUED", "WAITING", "COMPLETED", "FAILED", "CANCELLED"):
            self.snap(phase=phase, started_at=self.now - 10, last_activity_at=self.now - 10)
            self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now), phase)

    def test_bad_stamps_are_not(self):
        for stamp in (self.now + 60, float("nan"), float("inf"), "soon", True, None):
            self.snap(phase="RUNNING", started_at=None, last_activity_at=stamp)
            self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now), repr(stamp))

    def test_missing_corrupt_or_odd_snapshots_are_not(self):
        self.assertFalse(ab.in_progress(self.ws, "task-none", now=self.now))
        (self.ws / "state" / "activity" / "task-a.json").write_text("{not json")
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now))
        (self.ws / "state" / "activity" / "task-a.json").write_text("[1, 2]")
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now))

    def test_now_defaults_to_the_clock(self):
        self.snap(phase="RUNNING", started_at=time.time() - 5, last_activity_at=time.time() - 5)
        self.assertTrue(ab.in_progress(self.ws, "task-a"))

    def test_cli_answers_by_rc(self):
        self.snap(phase="RUNNING", started_at=time.time() - 5, last_activity_at=time.time() - 5)
        self.assertEqual(ab.main(["in-progress", "task-a", "--workspace", str(self.ws)]), 0)
        self.assertEqual(ab.main(["in-progress", "task-a", "--workspace", str(self.ws), "--max-age", "1"]), 1)
        self.assertEqual(ab.main(["in-progress", "task-none", "--workspace", str(self.ws)]), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ab.main(["in-progress"]), 2)


if __name__ == "__main__":
    unittest.main()
