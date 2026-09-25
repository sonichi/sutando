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
import unittest.mock
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
        (self.inbox / "task-mine-2.txt").write_text("")
        os.utime(self.inbox / "task-mine.txt", (1, 1))    # strictly older: order decided, not a same-second tie
        _task(self.ws / "tasks" / "task-mine-2.txt", "task-mine-2")

    def test_a_worker_inbox_counts_its_own_sentinels_not_the_cores_tasks(self):
        self.assertEqual(tq.waiting(self.ws, "task-mine", inbox=self.inbox), 1)
        # Membership, not order: two sentinels written in the same second tie on mtime.
        self.assertEqual(sorted(t["id"] for t in tq.pending(self.ws, self.inbox)), ["task-mine", "task-mine-2"])

    def test_the_core_reads_exactly_as_before(self):
        self.assertEqual(tq.waiting(self.ws, "task-mine"), 3)
        self.assertEqual(tq.waiting(self.ws, "task-mine", inbox=self.ws / "tasks"), 3)

    def test_a_sentinel_whose_task_has_a_ready_result_is_not_pending(self):
        # Built the way a host builds it: sentinels are written and never retired (they
        # stay 0-byte .txt entries); the only thing that changes is a result being published.
        (self.ws / "results").mkdir()
        for tid in ("task-old-a", "task-old-b", "task-old-c"):
            _task(self.ws / "tasks" / f"{tid}.txt", tid)
            (self.inbox / f"{tid}.txt").write_text("")
            (self.ws / "results" / f"{tid}.txt").write_text("done: answered earlier\n")
        (self.ws / "results" / "task-mine-2.txt").write_text("   \n")      # empty placeholder: not ready
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), sorted(
            ["task-mine.txt", "task-mine-2.txt", "task-old-a.txt", "task-old-b.txt", "task-old-c.txt"]))
        self.assertEqual(sorted(t["id"] for t in tq.pending(self.ws, self.inbox)), ["task-mine", "task-mine-2"])
        self.assertEqual(tq.waiting(self.ws, "task-mine", inbox=self.inbox), 1)
        (self.ws / "results" / "task-mine-2.txt").write_text("done: now answered\n")
        self.assertEqual(tq.waiting(self.ws, "task-mine", inbox=self.inbox), 0)
        # A renamed sentinel is not an entry at all, whichever suffix a future pruner picks.
        (self.inbox / "task-mine.txt").rename(self.inbox / "task-mine.accepted")
        self.assertEqual(tq.pending(self.ws, self.inbox), [])

    def _cli(self, *argv: str) -> tuple[int, str]:
        # In-process: the entry point's own branches are what run (and what is measured).
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = tq.main(list(argv))
        return rc, out.getvalue().strip()

    def test_the_cli_takes_inbox_and_does_not_overwrite_the_cores_snapshot(self):
        task_file = str(self.ws / "tasks" / "task-mine.txt")
        self.assertEqual(self._cli("waiting", "--task-file", task_file, "--inbox", str(self.inbox)), (0, "1"))
        self.assertFalse((self.ws / "state" / "task-queue.json").exists())
        rc, out = self._cli("pending", "--workspace", str(self.ws), "--inbox", str(self.inbox))
        self.assertEqual((rc, sorted(t["id"] for t in json.loads(out))), (0, ["task-mine", "task-mine-2"]))
        self.assertEqual(self._cli("position", "--task-file", task_file, "--inbox", str(self.inbox)),
                         (0, json.dumps({"depth": 2, "position": 1})))
        # The shipped core passes its own inbox, which is tasks/: same count, snapshot written.
        self.assertEqual(self._cli("waiting", "--task-file", task_file, "--inbox", str(self.ws / "tasks")), (0, "3"))
        snap = json.loads((self.ws / "state" / "task-queue.json").read_text())
        self.assertEqual(snap["depth"], 4)
        (self.ws / "state" / "task-queue.json").unlink()
        self.assertEqual(self._cli("waiting", "--task-file", task_file), (0, "3"))
        self.assertTrue((self.ws / "state" / "task-queue.json").exists())
        # The watcher reaches the same entry point through an interpreter path.
        r = subprocess.run([sys.executable, str(TQ), "waiting", "--task-file", task_file, "--inbox", str(self.inbox)],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "1"))


class ResolvedOncePerCall(unittest.TestCase):
    """Ready results are resolved in one pass per call, in every layout the per-id
    helper knows, and the number of directory listings does not grow with the inbox."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve()
        for d in ("tasks", "results", "results/archive", "results/archive/2026-08", "results/archive-2026-09-01", "state"):
            (self.ws / d).mkdir(parents=True)
        self.inbox = self.ws / "deliveries" / "w1"
        self.inbox.mkdir(parents=True)
        spec = importlib.util.spec_from_file_location("task_dispatch", REPO / "src" / "delivery" / "task_dispatch.py")
        self.td = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(self.td)

    def _sentinel(self, tid: str) -> None:
        _task(self.ws / "tasks" / f"{tid}.txt", tid)
        (self.inbox / f"{tid}.txt").write_text("")

    def test_every_result_layout_agrees_with_the_per_id_helper(self):
        r = self.ws / "results"
        layouts = {
            "task-live": r / "task-live.txt",
            "task-flat": r / "archive" / "task-flat.txt",
            "task-flat-epoch": r / "archive" / "task-flat-epoch-1790000000.txt",
            "task-month": r / "archive" / "2026-08" / "task-month.txt",
            "task-month-epoch": r / "archive" / "2026-08" / "task-month-epoch-1790000001.txt",
            "task-day": r / "archive-2026-09-01" / "task-day.txt",
            "task-day-epoch": r / "archive-2026-09-01" / "task-day-epoch-1790000002.txt",
        }
        for tid, path in layouts.items():
            self._sentinel(tid)
            path.write_text("done\n")
        self._sentinel("task-placeholder")                      # live empty, archived ready
        (r / "task-placeholder.txt").write_text("  \n")
        (r / "archive" / "task-placeholder.txt").write_text("done\n")
        self._sentinel("task-open")                             # no result anywhere
        self._sentinel("task-blank")                            # only a whitespace placeholder
        (r / "task-blank.txt").write_text("\n")
        names = sorted(p.name for p in self.inbox.iterdir())
        batch = self.td.ready_result_filenames(r, names)
        per_id = {n for n in names if self.td.has_ready_result(r, n)}
        self.assertEqual(batch, per_id)
        self.assertEqual(sorted(batch), sorted(f"{t}.txt" for t in list(layouts) + ["task-placeholder"]))
        self.assertEqual(sorted(t["id"] for t in tq.pending(self.ws, self.inbox)), ["task-blank", "task-open"])

    def test_directory_listings_do_not_grow_with_the_inbox(self):
        r = self.ws / "results"
        for i in range(400):                                    # a flat archive far larger than the inbox
            (r / "archive" / f"task-spent-{i}-1790000000.txt").write_text("done\n")
        for i in range(60):
            self._sentinel(f"task-spent-{i}")
        self._sentinel("task-open")
        calls = []
        real_scandir = os.scandir

        def counting_scandir(path=".", *a, **k):
            calls.append(str(path))
            return real_scandir(path, *a, **k)
        with unittest.mock.patch("os.scandir", counting_scandir):
            self.assertEqual([t["id"] for t in tq.pending(self.ws, self.inbox)], ["task-open"])
        # live results, archive/, its month dirs (1), the base for retention dirs (1), and the inbox itself.
        self.assertLessEqual(len(calls), 8, calls)


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
            # The core: its watcher names its inbox too, and that inbox is tasks/. The count is
            # the core's queue as before, and the snapshot is refreshed on the announcement.
            env["TASKS_DIR_ABS"] = str(ws / "tasks")
            self.assertFalse((ws / "state" / "task-queue.json").exists())
            r = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.stdout, "QUEUE: 2 pending after this\nEND\n", r.stderr)
            self.assertEqual(json.loads((ws / "state" / "task-queue.json").read_text())["depth"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
