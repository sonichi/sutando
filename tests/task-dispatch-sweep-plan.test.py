#!/usr/bin/env python3
"""task_dispatch.sweep_plan: one process decides which inbox entries a restart
sweep still dispatches, with the same verdicts the watcher's per-entry path reaches.

Run: python3 tests/task-dispatch-sweep-plan.test.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "worker-pool" / "scripts"))

from delivery import task_dispatch as td  # noqa: E402
import resolve_inbox_entry as rie  # noqa: E402

W = "0123456789abcdef0123456789abcdef"
RESOLVER = str(REPO / "skills" / "worker-pool" / "scripts" / "resolve-inbox-entry")
REFUSAL = "I could not safely process"


class Plan(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ws = Path(self._td.name).resolve()
        self.inbox = self.ws / "deliveries" / W
        self.results = self.ws / "results"
        for d in (self.inbox, self.ws / "tasks" / "archive", self.results):
            d.mkdir(parents=True, exist_ok=True)

    def ptr(self, tid, *, payload=True, age=0, priority="normal"):
        if payload:
            (self.ws / "tasks" / f"{tid}.txt").write_text(f"id: {tid}\npriority: {priority}\ntask: x\n")
        p = self.inbox / f"{tid}.txt"
        p.write_text("")
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    def plan(self, **kw):
        kw.setdefault("resolver", RESOLVER)
        kw.setdefault("workspace", str(self.ws))
        kw.setdefault("refusal_prefix", REFUSAL)
        return td.sweep_plan(self.inbox, self.results, **kw)

    def test_stale_pointers_drop_and_pending_ones_stay(self):
        self.ptr("task-old", payload=False, age=3600)
        self.ptr("task-new", payload=False)
        self.ptr("task-live", age=3600)
        keep, counts = self.plan()
        self.assertEqual(sorted(keep), ["task-live.txt", "task-new.txt"])
        self.assertEqual((counts["stale"], counts["batch"]), (1, "ok"))

    def test_an_answered_task_drops_but_a_refusal_or_placeholder_does_not(self):
        for tid, body in (("task-ans", "done\n"), ("task-ref", f"{REFUSAL} this: x\n"), ("task-empty", "  \n")):
            self.ptr(tid)
            (self.results / f"{tid}.txt").write_text(body)
        keep, counts = self.plan()
        self.assertEqual(sorted(keep), ["task-empty.txt", "task-ref.txt"])
        self.assertEqual(counts["answered"], 1)

    def test_an_archive_only_result_is_left_to_the_watcher_as_before(self):
        self.ptr("task-arch")
        (self.results / "archive").mkdir()
        (self.results / "archive" / "task-arch-1790000000.txt").write_text("done\n")
        keep, _ = self.plan()
        self.assertEqual(keep, ["task-arch.txt"])

    def test_priority_order_is_the_sort_order(self):
        self.ptr("task-low", priority="low")
        self.ptr("task-urgent", priority="urgent")
        keep, _ = self.plan()
        self.assertEqual(keep, [p.name for p in td.sort_tasks_by_priority(self.inbox.glob("*.txt"))])

    def test_a_resolver_without_batch_keeps_every_entry(self):
        old = self.ws / "old-resolver"
        old.write_text("#!/bin/sh\nexit 3\n")
        old.chmod(0o755)
        self.ptr("task-old", payload=False, age=3600)
        keep, counts = self.plan(resolver=str(old))
        self.assertEqual((keep, counts["batch"]), (["task-old.txt"], "unavailable"))

    def test_a_missing_resolver_keeps_every_entry(self):
        self.ptr("task-old", payload=False, age=3600)
        keep, counts = self.plan(resolver=str(self.ws / "nope"))
        self.assertEqual((keep, counts["batch"]), (["task-old.txt"], "unavailable"))

    def test_a_non_typed_failure_is_kept_whatever_its_age(self):
        flaky = self.ws / "flaky"
        flaky.write_text('#!/bin/sh\necho "resolve-inbox-entry batch v1"\n'
                         'while IFS= read -r e; do printf "1\\t\\t%s\\n" "$e"; done\n')
        flaky.chmod(0o755)
        self.ptr("task-x", age=3600)
        keep, _ = self.plan(resolver=str(flaky))
        self.assertEqual(keep, ["task-x.txt"])

    def test_no_resolver_filters_answered_core_tasks(self):
        tasks = self.ws / "tasks"
        (tasks / "task-a.txt").write_text("id: task-a\ntask: x\n")
        (tasks / "task-b.txt").write_text("id: task-b\ntask: x\n")
        (self.results / "task-a.txt").write_text("done\n")
        keep, counts = td.sweep_plan(tasks, self.results, refusal_prefix=REFUSAL)
        self.assertEqual((keep, counts["answered"]), (["task-b.txt"], 1))

    def test_cli_prints_absolute_entries_and_a_count_line(self):
        self.ptr("task-live")
        self.ptr("task-old", payload=False, age=3600)
        r = subprocess.run([sys.executable, str(REPO / "src" / "delivery" / "task_dispatch.py"), "sweep-plan",
                            str(self.inbox), str(self.results), "--resolver", RESOLVER, "--workspace", str(self.ws),
                            "--refusal-prefix", REFUSAL], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, f"{self.inbox}/task-live.txt\n{td.SWEEP_PLAN_DONE}\n")
        self.assertIn("sweep plan over 2 entries: 1 stale, 0 answered, 1 to dispatch", r.stderr)

    def test_batch_header_is_one_constant_on_both_sides(self):
        self.assertEqual(td.RESOLVER_BATCH_HEADER, rie.BATCH_HEADER)

    def test_the_watcher_checks_the_same_done_line(self):
        watcher = (REPO / "src" / "watch-tasks-stream.sh").read_text()
        self.assertEqual(watcher.count(f'"{td.SWEEP_PLAN_DONE}"'), 2)


class ResolverBatch(unittest.TestCase):
    def test_batch_gives_the_single_call_verdict_for_each_entry(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d).resolve()
            inbox = ws / "deliveries" / W
            inbox.mkdir(parents=True)
            (ws / "tasks").mkdir()
            (ws / "tasks" / "task-a.txt").write_text("id: task-a\n")
            for t in ("task-a", "task-b"):
                (inbox / f"{t}.txt").write_text("")
            entries = [str(inbox / "task-a.txt"), str(inbox / "task-b.txt"), str(inbox / "task-c.txt")]
            r = subprocess.run([RESOLVER, "--batch", "--workspace", str(ws)], input="\n".join(entries) + "\n",
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            lines = r.stdout.splitlines()
            self.assertEqual(lines[0], rie.BATCH_HEADER)
            got = {ln.split("\t")[2]: (ln.split("\t")[0], ln.split("\t")[1]) for ln in lines[1:]}
            for e in entries:
                single = subprocess.run([RESOLVER, e, "--workspace", str(ws)], capture_output=True, text=True)
                self.assertEqual(got[e], (str(single.returncode), single.stdout.strip()), e)


if __name__ == "__main__":
    unittest.main(verbosity=2)
