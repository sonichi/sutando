#!/usr/bin/env python3
"""src/task_queue.py: the pending list (owner tasks only, consumption order), a task's position, the
QUEUE count, the snapshot file, and the CLI that never fails its caller. Hermetic: a temp workspace.

Run: python3 tests/task-queue.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import task_queue as tq  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()
        self.t0 = time.time() - 100

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, tid, priority="normal", source="ag2space", age=0, with_id=True):
        p = self.ws / "tasks" / f"{tid}.txt"
        head = [f"id: {tid}"] if with_id else []
        p.write_text("\n".join(head + [f"source: {source}", f"priority: {priority}", "task: do it", "user_id: @o:s"]) + "\n")
        os.utime(p, (self.t0 + age, self.t0 + age))
        return p


class Pending(Base):
    def test_owner_tasks_in_consumption_order_and_bookkeeping_excluded(self):
        self.task("task-b", age=2); self.task("task-a", age=1); self.task("task-v", priority="urgent", source="voice", age=3)
        self.task("task-l", priority="low", source="cron", age=0)
        for name in ("task-cron-1", "task-bench-2", "task-workstream-3", "task-project-grouping-4"):
            self.task(name)
        (self.ws / "tasks" / "notes.md").write_text("not a task")
        (self.ws / "tasks" / "archive").mkdir()
        got = tq.pending(self.ws)
        self.assertEqual([t["id"] for t in got], ["task-v", "task-a", "task-b", "task-l"])
        self.assertEqual(got[0], {"id": "task-v", "source": "voice", "priority": "urgent", "since": int(self.t0 + 3)})
        self.assertTrue(all(set(t) == {"id", "source", "priority", "since"} for t in got))

    def test_the_id_header_wins_and_the_stem_is_the_fallback(self):
        self.task("task-x", with_id=False)
        self.assertEqual([t["id"] for t in tq.pending(self.ws)], ["task-x"])
        self.assertEqual(tq.is_queue_task("task-chat-1.txt"), True)
        self.assertEqual(tq.is_queue_task("task-cron-1.txt"), False)
        self.assertEqual(tq.is_queue_task("result-1.txt"), False)

    def test_a_missing_tasks_dir_is_an_empty_queue(self):
        self.assertEqual(tq.pending(self.ws / "nowhere"), [])


class Position(Base):
    def test_position_depth_and_the_waiting_count(self):
        self.task("task-1", age=1); self.task("task-2", age=2); self.task("task-3", age=3)
        self.assertEqual(tq.position(self.ws, "task-1"), {"depth": 3, "position": 1})
        self.assertEqual(tq.position(self.ws, "task-3"), {"depth": 3, "position": 3})
        self.assertEqual(tq.position(self.ws, "task-gone"), {"depth": 3, "position": 0})
        self.assertEqual(tq.waiting(self.ws, "task-1"), 2, "two others pending besides this one")
        self.assertEqual(tq.waiting(self.ws, "task-gone"), 3, "an already-taken task counts everyone still waiting")
        self.assertEqual(tq.waiting(self.ws / "nowhere", "task-1"), 0)

    def test_the_snapshot_is_the_same_list_under_state(self):
        self.task("task-1", age=1); self.task("task-2", age=2)
        p = tq.write_snapshot(self.ws)
        self.assertEqual(p, self.ws / "state" / "task-queue.json")
        snap = json.loads(p.read_text())
        self.assertEqual((snap["depth"], [t["id"] for t in snap["pending"]]), (2, ["task-1", "task-2"]))
        self.assertLessEqual(abs(snap["ts"] - time.time()), 5)
        self.assertFalse([f for f in (self.ws / "state").iterdir() if ".tmp" in f.name], "atomic write, no temp left")


class Cli(Base):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tq.main(list(argv))
        return rc, out.getvalue().strip(), err.getvalue()

    def test_waiting_prints_a_bare_integer_and_refreshes_the_snapshot(self):
        self.task("task-1", age=1); self.task("task-2", age=2)
        rc, out, _ = self.run_main("waiting", "--task-file", str(self.ws / "tasks" / "task-2.txt"))
        self.assertEqual((rc, out), (0, "1"))
        self.assertEqual(json.loads((self.ws / "state" / "task-queue.json").read_text())["depth"], 2)

    def test_position_and_pending_are_json_and_the_workspace_comes_from_the_file(self):
        self.task("task-1", age=1); self.task("task-2", age=2)
        rc, out, _ = self.run_main("position", "--task-file", str(self.ws / "tasks" / "task-2.txt"))
        self.assertEqual((rc, json.loads(out)), (0, {"depth": 2, "position": 2}))
        rc, out, _ = self.run_main("pending", "--workspace", str(self.ws))
        self.assertEqual([t["id"] for t in json.loads(out)], ["task-1", "task-2"])
        rc, out, _ = self.run_main("snapshot", "--workspace", str(self.ws))
        self.assertEqual((rc, out), (0, str(self.ws / "state" / "task-queue.json")))

    def test_the_cli_never_fails_its_caller(self):
        rc, out, _ = self.run_main("waiting", "--task-file", str(self.ws / "tasks" / "task-none.txt"))
        self.assertEqual((rc, out), (0, "0"), "a file that vanished still gets a number, from its dir")
        rc, out, _ = self.run_main("position", "--workspace", str(self.ws))
        self.assertEqual((rc, out), (0, ""), "no task id: nothing printed, exit 0")
        rc, out, _ = self.run_main("nonsense")
        self.assertEqual((rc, out), (0, ""))


class Tolerance(Base):
    """Every reader degrades instead of raising: the dispatch path never fails on a count."""

    def test_a_task_that_vanishes_mid_scan_still_counts(self):
        real = self.task("task-1", age=1)
        ghost = self.ws / "tasks" / "task-ghost.txt"
        with unittest.mock.patch.object(tq, "sort_tasks_by_priority", lambda paths: list(paths) + [ghost]):
            rows = tq.pending(self.ws)
        self.assertEqual([r["id"] for r in rows], ["task-1", "task-ghost"])
        ghost_row = rows[1]
        self.assertEqual((ghost_row["source"], ghost_row["priority"], ghost_row["since"]), (None, "normal", 0))
        self.assertEqual(rows[0]["since"], int(real.stat().st_mtime))

    def test_source_of_an_unreadable_file_is_none(self):
        self.assertIsNone(tq._source(self.ws / "tasks" / "absent.txt"))
        p = self.ws / "tasks" / "task-src.txt"
        p.write_text("id: task-src\ntask: hi\nsource: late\n")
        self.assertIsNone(tq._source(p), "a source line below task: is body text")

    def test_workspace_of_nothing_is_none(self):
        self.assertIsNone(tq._workspace_of(None, None))
        self.assertEqual(tq._workspace_of(None, str(self.ws)), self.ws)
        self.assertEqual(tq._workspace_of(str(self.ws / "tasks" / "t.txt"), None), self.ws.resolve())

    def test_an_unexpected_error_is_reported_on_stderr_and_still_exits_0(self):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(tq, "pending", side_effect=RuntimeError("disk on fire")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tq.main(["pending", "--workspace", str(self.ws)])
        self.assertEqual((rc, out.getvalue()), (0, ""))
        self.assertIn("task_queue: disk on fire", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=1)
