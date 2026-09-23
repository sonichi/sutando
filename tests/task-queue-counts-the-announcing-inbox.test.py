#!/usr/bin/env python3
"""The QUEUE line counts the announcing watcher's inbox, never the core's queue.

A pool worker's watcher reads `<workspace>/deliveries/<id>/`, where each entry is a
sentinel for a payload in the core's `tasks/`. Its `QUEUE: n pending after this` used
to count `tasks/` (the workspace derived from the payload's path), so a worker read the
core's in-flight tasks, and every other worker's, as its own queue. `task_queue` now
takes the inbox: the count is what waits there; the core, whose inbox is `tasks/`,
reads exactly as before.

Run: python3 tests/task-queue-counts-the-announcing-inbox.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TQ = REPO / "src" / "task_queue.py"
EMIT = REPO / "src" / "task-emit.sh"

spec = importlib.util.spec_from_file_location("task_queue", TQ)
tq = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(tq)


def _task(path: Path, tid: str, priority: str = "normal") -> None:
    path.write_text(f"id: {tid}\nsource: chat\npriority: {priority}\ntask: x\n")


class Counts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve()
        (self.ws / "tasks").mkdir()
        (self.ws / "state").mkdir()
        # Three payloads in the core's tasks/: one is the worker's, two are in flight elsewhere.
        for tid in ("task-mine", "task-core-a", "task-core-b"):
            _task(self.ws / "tasks" / f"{tid}.txt", tid)
        self.inbox = self.ws / "deliveries" / "w1"
        self.inbox.mkdir(parents=True)
        (self.inbox / "task-mine.txt").write_text("")     # the sentinel being announced
        (self.inbox / "task-mine-2.txt").write_text("")   # one more waiting in the worker's inbox
        _task(self.ws / "tasks" / "task-mine-2.txt", "task-mine-2")

    def test_a_worker_inbox_counts_its_own_sentinels_not_the_cores_tasks(self):
        self.assertEqual(tq.waiting(self.ws, "task-mine", inbox=self.inbox), 1)
        self.assertEqual([t["id"] for t in tq.pending(self.ws, self.inbox)], ["task-mine", "task-mine-2"])

    def test_the_core_reads_exactly_as_before(self):
        self.assertEqual(tq.waiting(self.ws, "task-mine"), 3)
        self.assertEqual(tq.waiting(self.ws, "task-mine", inbox=self.ws / "tasks"), 3)

    def test_an_accepted_sentinel_is_not_pending(self):
        (self.inbox / "task-mine-2.txt").rename(self.inbox / "task-mine-2.accepted")
        self.assertEqual(tq.waiting(self.ws, "task-mine", inbox=self.inbox), 0)

    def test_the_cli_takes_inbox_and_does_not_overwrite_the_cores_snapshot(self):
        r = subprocess.run([sys.executable, str(TQ), "waiting", "--task-file", str(self.ws / "tasks" / "task-mine.txt"),
                            "--inbox", str(self.inbox)], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "1"))
        self.assertFalse((self.ws / "state" / "task-queue.json").exists())
        r = subprocess.run([sys.executable, str(TQ), "waiting", "--task-file", str(self.ws / "tasks" / "task-mine.txt")],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "3"))
        snap = json.loads((self.ws / "state" / "task-queue.json").read_text())
        self.assertEqual(snap["depth"], 4)


class EmitPassesTheInbox(unittest.TestCase):
    """The shipped queue_line, extracted from task-emit.sh, hands its inbox to the counter."""

    def test_queue_line_counts_the_watchers_inbox(self):
        text = EMIT.read_text()
        m = re.search(r"^queue_line\(\) \{.*?^\}", text, re.S | re.M)
        assert m, "queue_line not found"
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td).resolve()
            (ws / "tasks").mkdir(); (ws / "state").mkdir()
            inbox = ws / "deliveries" / "w1"; inbox.mkdir(parents=True)
            for tid in ("task-mine", "task-core-a", "task-core-b"):
                _task(ws / "tasks" / f"{tid}.txt", tid)
            (inbox / "task-mine.txt").write_text("")
            # A stub for the activity lookup the function starts with: the resolved payload path.
            harness = "\n".join([
                f'_activity_task_file() {{ printf "%s" "{ws}/tasks/task-mine.txt"; }}',
                m.group(0).replace('"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/task_queue.py"', f'"{TQ}"'),
                "queue_line task-mine.txt; echo END",
            ])
            env = {**os.environ, "TASKS_DIR_ABS": str(inbox), "SUTANDO_PY_BIN": sys.executable}
            r = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.stdout, "END\n", r.stderr)      # nothing else waits in the worker's inbox
            (inbox / "task-mine-2.txt").write_text("")
            r = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.stdout, "QUEUE: 1 pending after this\nEND\n", r.stderr)
            env.pop("TASKS_DIR_ABS")                            # the core: no inbox, tasks/ as before
            r = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.stdout, "QUEUE: 2 pending after this\nEND\n", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
