#!/usr/bin/env python3
"""pool_follower (L2): assignment honoring, no-steal under a live lead,
leaderless fallback on a stale/absent/future-dated lead beat.

Run: python3 tests/pool-follower-acquire.test.py   (stdlib only)
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

import pool_follower as pf  # noqa: E402


class AcquireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        (self.state / "cores").mkdir(parents=True)
        self.tasks.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _beat(self, label="lead", age=0):
        f = self.state / "cores" / f"{label}.alive"
        f.write_text("{}")
        t = time.time() - age
        os.utime(f, (t, t))

    def test_own_assignment_claimed_in_priority_order(self):
        (self.tasks / "task-a.assigned-me.txt").write_text("priority: low\n")
        (self.tasks / "task-b.assigned-me.txt").write_text("priority: urgent\n")
        self._beat()
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-b.claimed-me.txt")

    def test_other_followers_assignment_never_touched(self):
        (self.tasks / "task-a.assigned-peer.txt").write_text("x")
        self._beat()
        self.assertIsNone(pf.acquire_work(self.tasks, self.state, "me", "lead"))
        self.assertTrue((self.tasks / "task-a.assigned-peer.txt").exists())

    def test_live_lead_owns_the_unassigned_pool(self):
        (self.tasks / "task-free.txt").write_text("task: t\n")
        self._beat(age=10)
        self.assertIsNone(pf.acquire_work(self.tasks, self.state, "me", "lead"))
        self.assertTrue((self.tasks / "task-free.txt").exists())

    def test_stale_lead_falls_back_to_leaderless_claim(self):
        (self.tasks / "task-free.txt").write_text("task: t\n")
        self._beat(age=pf.LEAD_STALE_S + 5)
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-free.claimed-me.txt")

    def test_absent_lead_beat_also_falls_back(self):
        (self.tasks / "task-free.txt").write_text("task: t\n")
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-free.claimed-me.txt")

    def test_future_dated_lead_beat_degrades(self):
        # clock skew: a lead "from the future" is not a live lead
        f = self.state / "cores" / "lead.alive"
        f.write_text("{}")
        t = time.time() + 3600
        os.utime(f, (t, t))
        (self.tasks / "task-free.txt").write_text("task: t\n")
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-free.claimed-me.txt")

    def test_small_future_skew_keeps_live_lead_in_control(self):
        self._beat(age=-0.5)
        (self.tasks / "task-free.txt").write_text("task: t\n")
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertIsNone(got)
        self.assertTrue((self.tasks / "task-free.txt").exists())

    def test_assignments_still_honored_in_fallback_mode(self):
        (self.tasks / "task-mine.assigned-me.txt").write_text("x")
        (self.tasks / "task-free.txt").write_text("task: t\n")
        got = pf.acquire_work(self.tasks, self.state, "me", "lead")
        self.assertEqual(got.name, "task-mine.claimed-me.txt")

    def test_fallback_never_takes_claimed_or_assigned_files(self):
        (self.tasks / "task-x.claimed-peer.txt").write_text("x")
        (self.tasks / "task-y.assigned-peer.txt").write_text("x")
        self.assertIsNone(pf.acquire_work(self.tasks, self.state, "me", "lead"))
        self.assertEqual(len(list(self.tasks.iterdir())), 2)


class AcquireCliTests(unittest.TestCase):
    """The CLI is what SKILL.md tells a follower to run. acquire_work() being
    correct proves nothing if no shipped entry point reaches it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()
        (self.ws / "state" / "cores").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *argv):
        out = subprocess.run(
            [sys.executable, str(REPO / "src" / "pool_follower.py"), *argv],
            capture_output=True, text=True, timeout=30)
        return out.returncode, out.stdout.strip(), out.stderr.strip()

    def _lead_beat(self):
        f = self.ws / "state" / "cores" / f"{pf.LEAD_LABEL}.alive"
        f.write_text("{}")
        t = time.time()
        os.utime(f, (t, t))

    def test_acquire_claims_an_assignment_under_a_live_lead(self):
        # The production shape: the lead assigned it, the follower must take it.
        (self.ws / "tasks" / "task-demo.assigned-core-1.txt").write_text("hi")
        self._lead_beat()
        rc, out, _ = self._run("acquire", str(self.ws / "tasks"), "core-1")
        self.assertEqual(rc, 0)
        self.assertEqual(Path(out).name, "task-demo.claimed-core-1.txt")
        self.assertTrue(Path(out).exists())

    def test_default_lead_label_is_the_one_the_daemon_writes(self):
        # A wrong default silently reads a missing beat, so the follower thinks
        # the lead is dead and starts taking unassigned work it must not touch.
        (self.ws / "tasks" / "task-free.txt").write_text("hi")
        self._lead_beat()  # writes LEAD_LABEL.alive
        rc, _, _ = self._run("acquire", str(self.ws / "tasks"), "core-1")
        self.assertEqual(rc, 1, "a live lead owns the unassigned pool")
        self.assertTrue((self.ws / "tasks" / "task-free.txt").exists())

    def test_idle_is_exit_1_not_a_crash(self):
        self._lead_beat()
        rc, out, err = self._run("acquire", str(self.ws / "tasks"), "core-1")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)

    def test_bad_usage_is_exit_2(self):
        self.assertEqual(self._run("acquire")[0], 2)
        self.assertEqual(self._run("acquire", "/nope/nope", "core-1")[0], 2)
        self.assertEqual(self._run("bogus")[0], 2)

    def test_skill_md_documents_a_command_that_actually_runs(self):
        # The defect this file exists for: the documented invocation named a
        # file that does not exist, so no follower could ever acquire.
        skill = (REPO / "skills" / "proactive-loop-pool" / "SKILL.md").read_text()
        self.assertIn("pool_follower.py acquire", skill)
        self.assertNotIn("pool_follower (acquire_work).py", skill)


class LeadLabelTests(unittest.TestCase):
    def test_daemon_imports_the_label_rather_than_redefining_it(self):
        src = (REPO / "scripts" / "pool-lead-daemon.py").read_text()
        self.assertIn("LEAD_LABEL", src)
        self.assertNotIn('LEAD_LABEL = "', src,
                         "second definition drifts from pool_follower's")


if __name__ == "__main__":
    unittest.main(verbosity=2)
