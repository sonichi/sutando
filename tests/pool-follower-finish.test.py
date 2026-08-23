#!/usr/bin/env python3
"""finish_task (pairing fix): a composed body binds to its task via the
`task: <id>` echo line — mismatch/foreign-claim/empty bodies are refused
with ZERO writes (the two-claims body-swap incident repro is here).

Run: python3 tests/pool-follower-finish.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import tempfile
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
        self.assertTrue((self.tasks / "archive" / claimed.name).exists())
        self.assertEqual(list(self.results.glob(".task-*")), [])  # no tmp left

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

        def spy(src, dst):
            calls.append((str(dst), flag.exists()))
            return real_replace(src, dst)

        pf.os.replace = spy
        try:
            self._finish(self._claim("a1"), "task: a1\nbody\n")
        finally:
            pf.os.replace = real_replace
        self.assertTrue(flag.exists())
        # replace #1 = result (flag NOT yet written), #2 = archive (flag is)
        self.assertIn("results/task-a1.txt", calls[0][0])
        self.assertFalse(calls[0][1])
        self.assertIn("archive", calls[1][0])
        self.assertTrue(calls[1][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
