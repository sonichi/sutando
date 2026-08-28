#!/usr/bin/env python3
"""finish_task (pairing fix): a composed body binds to its task via the
`task: <id>` echo line — mismatch/foreign-claim/empty bodies are refused
with ZERO writes (the two-claims body-swap incident repro is here).

Run: python3 tests/pool-follower-finish.test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import pool_follower as pf  # noqa: E402


class FinishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.results = root / "results"
        self.state = root / "state"
        self.tasks.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _claim(self, task_id, instance="me"):
        f = self.tasks / f"task-{task_id}.claimed-{instance}.txt"
        f.write_text(f"id: task-{task_id}\ntask: do the thing\n")
        return f

    def _finish(self, claimed, body, instance="me"):
        return pf.finish_task(self.tasks, self.results, self.state,
                              instance, claimed, body)

    def _assert_nothing_written(self):
        self.assertFalse(self.results.exists())
        self.assertFalse((self.state / "cores").exists())
        self.assertFalse((self.tasks / "archive").exists())

    def test_correct_flow_writes_result_flag_and_archives(self):
        claimed = self._claim("a1")
        out = self._finish(claimed, "task: a1\nAnswer for the owner.\n")
        self.assertEqual(out, self.results / "task-a1.txt")
        # echo line stripped — the user never sees the pairing header
        self.assertEqual(out.read_text(), "Answer for the owner.\n")
        self.assertTrue(
            (self.state / "cores" / "me" / "done" / "task-a1.flag").exists())
        self.assertFalse(claimed.exists())
        # canonical archive name — result consumers resolve by task-<id>.txt;
        # a claimed-suffix name dead-letters the reply as no-task (incident)
        self.assertTrue((self.tasks / "archive" / "task-a1.txt").exists())
        self.assertFalse((self.tasks / "archive" / claimed.name).exists())
        self.assertEqual(list(self.results.glob(".task-*")), [])  # no tmp left

    def test_archive_never_clobbers_existing_record(self):
        archive = self.tasks / "archive"
        archive.mkdir()
        (archive / "task-a1.txt").write_text("earlier archived record\n")
        claimed = self._claim("a1")
        self._finish(claimed, "task: a1\nAnswer.\n")
        self.assertEqual((archive / "task-a1.txt").read_text(),
                         "earlier archived record\n")
        self.assertEqual((archive / "task-a1.txt.1").read_text(),
                         "id: task-a1\ntask: do the thing\n")
        self.assertFalse(claimed.exists())

    def test_incident_repro_body_of_a_against_claim_of_b_refused(self):
        # Two concurrent claims; A's composed body handed B's path must
        # refuse — this exact swap sent an owner's answer to the wrong room.
        self._claim("a1")
        claim_b = self._claim("b2")
        body_for_a = "task: a1\nPrivate answer meant for room A.\n"
        with self.assertRaises(ValueError):
            self._finish(claim_b, body_for_a)
        self._assert_nothing_written()
        self.assertTrue(claim_b.exists())

    def test_foreign_instance_claim_refused(self):
        claimed = self._claim("a1", instance="peer")
        with self.assertRaises(ValueError):
            self._finish(claimed, "task: a1\nbody\n", instance="me")
        self._assert_nothing_written()
        self.assertTrue(claimed.exists())

    def test_empty_body_refused(self):
        claimed = self._claim("a1")
        for body in ("", "   \n\n"):
            with self.assertRaises(ValueError):
                self._finish(claimed, body)
        self._assert_nothing_written()

    def test_missing_echo_line_refused(self):
        claimed = self._claim("a1")
        with self.assertRaises(ValueError):
            self._finish(claimed, "Answer without the pairing line.\n")
        self._assert_nothing_written()

    def test_echo_only_body_refused(self):
        claimed = self._claim("a1")
        with self.assertRaises(ValueError):
            self._finish(claimed, "task: a1\n")
        self._assert_nothing_written()

    def test_missing_claimed_file_refused(self):
        ghost = self.tasks / "task-a1.claimed-me.txt"
        with self.assertRaises(ValueError):
            self._finish(ghost, "task: a1\nbody\n")
        self._assert_nothing_written()

    def test_result_written_before_flag_ordering_hardcoded(self):
        # flag write must come after the result lands (crash between the
        # two must look like "no result yet", never "done without result")
        flag = self.state / "cores" / "me" / "done" / "task-a1.flag"
        calls = []
        real_replace = pf.os.replace
        real_move = pf._move_without_clobbering

        def spy_replace(src, dst):
            calls.append((str(dst), flag.exists()))
            return real_replace(src, dst)

        def spy_move(src, dst):
            calls.append((str(dst), flag.exists()))
            return real_move(src, dst)

        pf.os.replace = spy_replace
        pf._move_without_clobbering = spy_move
        try:
            self._finish(self._claim("a1"), "task: a1\nbody\n")
        finally:
            pf.os.replace = real_replace
            pf._move_without_clobbering = real_move
        self.assertTrue(flag.exists())
        # move #1 = result (flag NOT yet written), #2 = archive (flag is)
        self.assertIn("results/task-a1.txt", calls[0][0])
        self.assertFalse(calls[0][1])
        self.assertIn("archive", calls[1][0])
        self.assertTrue(calls[1][1])


class MetricsTests(FinishTests):
    """Per-task completion record — the pool's only source of duration."""

    def _metrics(self):
        return Path(self.tmp.name) / "data" / "pool-metrics.jsonl"

    def _finish_m(self, claimed, body, instance="me"):
        return pf.finish_task(self.tasks, self.results, self.state,
                              instance, claimed, body, self._metrics())

    def test_no_metrics_path_writes_no_file(self):
        self._finish(self._claim("a1"), "task: a1\nbody\n")
        self.assertFalse(self._metrics().exists())

    def test_record_carries_core_source_and_duration(self):
        claimed = self._claim("a1")
        claimed.write_text("id: task-a1\nsource: ag2space\ntask: x\n")
        os.utime(claimed, (time.time() - 30, time.time() - 30))
        self._finish_m(claimed, "task: a1\nbody\n")
        rec = json.loads(self._metrics().read_text().strip())
        self.assertEqual(rec["task_id"], "a1")
        self.assertEqual(rec["core"], "me")
        self.assertEqual(rec["source"], "ag2space")
        # arrival survives the assign/claim renames, so duration is real
        self.assertGreaterEqual(rec["duration_s"], 30)
        self.assertLess(rec["duration_s"], 120)

    def test_appends_one_line_per_task(self):
        for tid in ("a1", "b2", "c3"):
            self._finish_m(self._claim(tid), f"task: {tid}\nbody\n")
        lines = self._metrics().read_text().strip().splitlines()
        self.assertEqual([json.loads(x)["task_id"] for x in lines],
                         ["a1", "b2", "c3"])

    def test_refused_finish_records_nothing(self):
        with self.assertRaises(ValueError):
            self._finish_m(self._claim("a1"), "task: b2\nbody\n")
        self.assertFalse(self._metrics().exists())

    def test_unwritable_metrics_path_does_not_fail_the_task(self):
        # bookkeeping must never turn a delivered answer into a failure
        blocked = Path(self.tmp.name) / "blocked"
        blocked.write_text("not a directory")
        out = pf.finish_task(self.tasks, self.results, self.state, "me",
                             self._claim("a1"), "task: a1\nbody\n",
                             blocked / "sub" / "m.jsonl")
        self.assertTrue(out.exists())
        self.assertTrue((self.tasks / "archive" / "task-a1.txt").exists())


class DefensiveReadTests(unittest.TestCase):
    """The two OSError arms. Both are bookkeeping — neither may fail a task
    that has already completed, which is exactly what makes them easy to leave
    unexercised."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_unreadable_claim_yields_no_source_not_an_error(self):
        # A directory where the claim should be: read_text raises OSError.
        d = self.root / "task-x.claimed-core-1.txt"
        d.mkdir()
        self.assertEqual(pf._source_of(d), "")

    def test_a_readable_claim_still_yields_its_source(self):
        # Positive control: without it, a _source_of that returned "" always
        # would satisfy the assertion above.
        f = self.root / "task-y.claimed-core-1.txt"
        f.write_text("id: task-y\nsource: ag2space\n\nbody\n")
        self.assertEqual(pf._source_of(f), "ag2space")

    def test_a_blank_line_ends_the_header_scan(self):
        # `source:` below the blank line belongs to the BODY, not the header
        # block, so it must not be read as the task's source.
        f = self.root / "task-w.claimed-core-1.txt"
        f.write_text("id: task-w\n\nsource: forged-by-the-body\n")
        self.assertEqual(pf._source_of(f), "")

    def test_unstattable_claim_records_no_arrival_but_still_finishes(self):
        tasks, results, state = (self.root / "tasks", self.root / "results",
                                 self.root / "state")
        tasks.mkdir()
        claimed = tasks / "task-z.claimed-core-1.txt"
        claimed.write_text("id: task-z\nsource: chat\n\nbody\n")
        metrics = self.root / "data" / "pool-metrics.jsonl"

        real_stat = Path.stat

        def stat(self, *a, **kw):
            if self.name == "task-z.claimed-core-1.txt":
                raise OSError(5, "simulated stat failure")
            return real_stat(self, *a, **kw)

        Path.stat = stat
        try:
            out = pf.finish_task(tasks, results, state, "core-1", claimed,
                                 "task: z\nthe answer\n", metrics)
        finally:
            Path.stat = real_stat

        self.assertTrue(out.exists(), "the result is still written")
        rec = json.loads(metrics.read_text().strip())
        self.assertIsNone(rec["arrived_at"], "arrival is unknown, not invented")
        self.assertIsNone(rec["duration_s"], "and duration stays unknown too")


if __name__ == "__main__":
    unittest.main(verbosity=2)
