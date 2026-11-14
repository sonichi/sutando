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

    def row(self, task="task-a", kind="working", ts=None, projection=None):
        rec = {"ts": self.now - 10 if ts is None else ts, "line": "x", "kind": kind, "task": {"id": task}}
        if projection:
            rec["projection"] = projection
        with open(self.ws / "state" / "agent-activity.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def test_delivered_but_never_engaged_is_not_in_progress(self):
        # The watcher marks RUNNING at hand-off (task-emit.sh); the session may never
        # have touched the task. Review of #4863: delivery is not work.
        self.snap(phase="RUNNING", started_at=self.now - 3000, last_activity_at=self.now - 60)
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now))
        # The bus's own delivery row does not count as engagement either.
        self.row(kind="processing", projection="TASK_STATUS")
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now))

    def test_a_task_read_or_thought_about_but_never_worked_on_still_blocks(self):
        # Review of #4863 (Rui): a read-then-drop must not pass. Only a tool call the hook
        # attributed to the task (a `working` row) or a runtime event is work.
        self.snap(phase="RUNNING", started_at=self.now - 3000, last_activity_at=self.now - 60)
        self.row(kind="processing", ts=self.now - 30)
        self.row(kind="thinking", ts=self.now - 20)
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now))
        self.row(kind="working", ts=self.now - 10)
        self.assertTrue(ab.in_progress(self.ws, "task-a", now=self.now))

    def test_a_fresh_working_row_from_the_session_hook_is_engagement(self):
        self.snap(phase="RUNNING", started_at=self.now - 3000, last_activity_at=self.now - 60)
        self.row(kind="working", ts=self.now - 30)
        self.assertTrue(ab.in_progress(self.ws, "task-a", now=self.now))
        self.row(task="task-b", ts=self.now - 1)
        self.assertFalse(ab.in_progress(self.ws, "task-b", now=self.now), "a row for a task with no RUNNING snapshot")

    def test_a_runtime_event_on_the_snapshot_is_engagement(self):
        self.snap(phase="RUNNING", seq=3, started_at=self.now - 5000, last_activity_at=self.now - 10)
        self.assertTrue(ab.in_progress(self.ws, "task-a", now=self.now))
        self.snap(phase="RUNNING", seq=0, started_at=self.now - 10, last_activity_at=self.now - 10)
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now), "seq 0: nothing observed from a session")

    def test_stale_engagement_is_not(self):
        self.snap(phase="RUNNING", started_at=self.now - 4000, last_activity_at=self.now - 4000)
        self.row(ts=self.now - 4000)
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now))
        self.assertTrue(ab.in_progress(self.ws, "task-a", max_age=5000, now=self.now))
        self.snap(phase="RUNNING", seq=1, started_at=self.now - 4000, last_activity_at=self.now - 4000)
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now))

    def test_other_phases_are_not(self):
        for phase in ("RECEIVED", "QUEUED", "WAITING", "COMPLETED", "FAILED", "CANCELLED"):
            self.snap(phase=phase, seq=2, started_at=self.now - 10, last_activity_at=self.now - 10)
            self.row(ts=self.now - 5)
            self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now), phase)

    def test_bad_stamps_are_not(self):
        for stamp in (self.now + 60, float("nan"), float("inf"), "soon", True, None):
            self.snap(phase="RUNNING", seq=1, started_at=None, last_activity_at=stamp)
            self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now), repr(stamp))
        (self.ws / "state" / "agent-activity.jsonl").write_text("not json\n" + json.dumps({"ts": "x", "kind": "working", "task": {"id": "task-a"}}) + "\n")
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now), "unparseable or unstamped rows are not engagement")

    def test_missing_corrupt_or_odd_snapshots_are_not(self):
        self.assertFalse(ab.in_progress(self.ws, "task-none", now=self.now))
        (self.ws / "state" / "activity" / "task-a.json").write_text("{not json")
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now))
        (self.ws / "state" / "activity" / "task-a.json").write_text("[1, 2]")
        self.assertFalse(ab.in_progress(self.ws, "task-a", now=self.now))

    def test_now_defaults_to_the_clock(self):
        self.snap(phase="RUNNING", seq=1, started_at=time.time() - 5, last_activity_at=time.time() - 5)
        self.assertTrue(ab.in_progress(self.ws, "task-a"))

    def test_cli_answers_by_rc(self):
        self.snap(phase="RUNNING", seq=1, started_at=time.time() - 5, last_activity_at=time.time() - 5)
        self.assertEqual(ab.main(["in-progress", "task-a", "--workspace", str(self.ws)]), 0)
        self.assertEqual(ab.main(["in-progress", "task-a", "--workspace", str(self.ws), "--max-age", "1"]), 1)
        self.assertEqual(ab.main(["in-progress", "task-none", "--workspace", str(self.ws)]), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ab.main(["in-progress"]), 2)


if __name__ == "__main__":
    unittest.main()
