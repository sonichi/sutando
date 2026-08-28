#!/usr/bin/env python3
"""pool_follower (L2): assignment honoring, no-steal under a live lead,
leaderless fallback on a stale/absent/future-dated lead beat.

Run: python3 tests/pool-follower-acquire.test.py   (stdlib only)
"""
from __future__ import annotations

import os
import contextlib
import io
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
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


class AcquireCliInProcessTests(unittest.TestCase):
    """Calls _acquire_cli directly. The subprocess cases below prove the SHIPPED
    entry point works; a subprocess runs outside the coverage instrumentation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()
        (self.ws / "state" / "cores").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pf._acquire_cli(list(argv))
        return rc, out.getvalue().strip(), err.getvalue().strip()

    def _lead_beat(self):
        f = self.ws / "state" / "cores" / f"{pf.LEAD_LABEL}.alive"
        f.write_text("{}")
        t = time.time()
        os.utime(f, (t, t))

    def test_claims_its_own_assignment(self):
        (self.ws / "tasks" / "task-a.assigned-core-1.txt").write_text("x")
        self._lead_beat()
        rc, out, _ = self._call(str(self.ws / "tasks"), "core-1")
        self.assertEqual(rc, 0)
        self.assertEqual(Path(out).name, "task-a.claimed-core-1.txt")

    def test_idle_is_1_with_no_output(self):
        self._lead_beat()
        rc, out, _ = self._call(str(self.ws / "tasks"), "core-1")
        self.assertEqual((rc, out), (1, ""))

    def test_live_lead_owns_the_unassigned_pool(self):
        (self.ws / "tasks" / "task-free.txt").write_text("x")
        self._lead_beat()
        self.assertEqual(self._call(str(self.ws / "tasks"), "core-1")[0], 1)
        self.assertTrue((self.ws / "tasks" / "task-free.txt").exists())

    def test_explicit_lead_label_is_honoured(self):
        (self.ws / "tasks" / "task-free.txt").write_text("x")
        self._lead_beat()  # beats LEAD_LABEL, not "other-lead"
        rc, out, _ = self._call(str(self.ws / "tasks"), "core-1", "other-lead")
        self.assertEqual(rc, 0, "an absent beat for the NAMED lead means leaderless")
        self.assertEqual(Path(out).name, "task-free.claimed-core-1.txt")

    def test_wrong_arity_is_2_and_prints_usage(self):
        rc, _, err = self._call(str(self.ws / "tasks"))
        self.assertEqual(rc, 2)
        self.assertIn("pool_follower.py acquire", err)
        self.assertEqual(self._call("a", "b", "c", "d")[0], 2)

    def test_missing_tasks_dir_is_2_not_a_crash(self):
        rc, _, err = self._call(str(self.ws / "nope"), "core-1")
        self.assertEqual(rc, 2)
        self.assertIn("not a directory", err)


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

    def test_exactly_one_acquisition_invocation_is_documented(self):
        # Three ways to acquire, one claiming to supersede the others, made
        # claiming depend on how far down the file a follower read.
        skill = (REPO / "skills" / "proactive-loop-pool" / "SKILL.md").read_text()
        self.assertEqual(skill.count("pool_follower.py acquire"), 1)
        self.assertEqual(skill.count("from pool_follower import acquire_work"), 0)
        self.assertEqual(skill.count("python3 src/claim_task.py"), 0)

    def test_skill_md_documents_a_command_that_actually_runs(self):
        # The defect this file exists for: the documented invocation named a
        # file that does not exist, so no follower could ever acquire.
        skill = (REPO / "skills" / "proactive-loop-pool" / "SKILL.md").read_text()
        self.assertIn("pool_follower.py acquire", skill)
        self.assertNotIn("pool_follower (acquire_work).py", skill)


class DegradedFilesystemTests(unittest.TestCase):
    """A follower must degrade on filesystem trouble, never crash: a raising
    tick takes the whole session down and the queue stops draining."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.tasks = self.ws / "tasks"
        self.state = self.ws / "state"
        self.tasks.mkdir()
        (self.state / "cores").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_lost_assignment_race_is_idle_not_an_error(self):
        # The lead reclaimed it, or a restart raced us. Returning None lets the
        # caller try the next file; raising would end the tick.
        f = self.tasks / "task-gone.assigned-core-1.txt"
        f.write_text("x")
        f.unlink()
        self.assertIsNone(pf._claim_assignment(self.tasks, f, "core-1"))

    def test_an_unreadable_tasks_dir_yields_no_work_in_both_modes(self):
        self.tasks.chmod(0o000)
        try:
            # live lead: the assignment scan fails, and the fallback pool is
            # not opened at all, so the answer is None either way
            beat = self.state / "cores" / f"{pf.LEAD_LABEL}.alive"
            beat.write_text("{}")
            self.assertIsNone(
                pf.acquire_work(self.tasks, self.state, "core-1", pf.LEAD_LABEL))
            beat.unlink()  # leaderless: the fallback scan is what now fails
            self.assertIsNone(
                pf.acquire_work(self.tasks, self.state, "core-1", pf.LEAD_LABEL))
        finally:
            self.tasks.chmod(0o700)

    def test_a_lost_fallback_race_moves_on_to_the_next_candidate(self):
        # Leaderless mode, two claimable files, the first rename loses.
        (self.tasks / "task-a.txt").write_text("x")
        (self.tasks / "task-b.txt").write_text("x")
        real, seen = os.rename, []

        def flaky(src, dst):
            if not seen:
                seen.append(src)
                raise OSError("lost the race")
            return real(src, dst)

        with mock.patch.object(pf.os, "rename", side_effect=flaky):
            got = pf.acquire_work(self.tasks, self.state, "core-1", pf.LEAD_LABEL)
        self.assertIsNotNone(got, "one loss ended the sweep instead of continuing")
        self.assertTrue(got.name.endswith(".claimed-core-1.txt"))


class FinishCliTests(unittest.TestCase):
    """Exit codes are the follower contract. Driven in-process: a subprocess
    run proves the same behaviour but leaves the shipped lines unmeasured."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()
        (self.ws / "results").mkdir()
        (self.ws / "state").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _claimed(self):
        p = self.ws / "tasks" / "task-t1.claimed-core-9.txt"
        p.write_text("id: task-t1\n")
        return p

    def test_wrong_argument_count_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(pf._finish_cli(["only-one"]), 2)
        self.assertIn("usage:", err.getvalue())

    def test_a_body_for_another_task_is_refused_not_written(self):
        claimed = self._claimed()
        err = io.StringIO()
        with (contextlib.redirect_stderr(err),
              mock.patch.object(sys, "stdin", io.StringIO("task: OTHER\nbody\n"))):
            self.assertEqual(pf._finish_cli([str(claimed), "core-9"]), 2)
        self.assertIn("refused", err.getvalue())
        self.assertFalse((self.ws / "results" / "task-t1.txt").exists())

    def test_a_paired_body_is_published_and_its_path_printed(self):
        claimed = self._claimed()
        out = io.StringIO()
        with (contextlib.redirect_stdout(out),
              mock.patch.object(sys, "stdin", io.StringIO("task: t1\nthe answer\n"))):
            self.assertEqual(pf._finish_cli([str(claimed), "core-9"]), 0)
        published = self.ws / "results" / "task-t1.txt"
        # _finish_cli resolves its argument; on macOS /var is a symlink.
        self.assertEqual(Path(out.getvalue().strip()).resolve(), published.resolve())
        self.assertEqual(published.read_text(), "the answer\n")


class FinishRefusalTests(unittest.TestCase):
    """Every refusal writes nothing. A publish that half-happens is worse than
    one that never starts, because the done flag and the archive follow it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        for d in ("tasks", "results", "state"):
            (self.ws / d).mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _finish(self, name, body, instance="core-9", create=True):
        claimed = self.ws / "tasks" / name
        if create:
            claimed.write_text("id: task-t1\n")
        with self.assertRaises(ValueError) as caught:
            pf.finish_task(self.ws / "tasks", self.ws / "results",
                           self.ws / "state", instance, claimed, body)
        self.assertFalse(any((self.ws / "results").iterdir()), "wrote on refusal")
        return str(caught.exception)

    def test_a_claim_another_instance_holds_is_refused(self):
        msg = self._finish("task-t1.claimed-core-2.txt", "task: t1\nbody\n")
        self.assertIn("not a claim held by", msg)

    def test_a_vanished_claim_file_is_refused(self):
        msg = self._finish("task-t1.claimed-core-9.txt", "task: t1\nbody\n",
                           create=False)
        self.assertIn("claimed file missing", msg)

    def test_an_empty_body_is_refused(self):
        msg = self._finish("task-t1.claimed-core-9.txt", "   \n")
        self.assertIn("empty result body", msg)

    def test_a_body_that_is_only_the_pairing_echo_is_refused(self):
        msg = self._finish("task-t1.claimed-core-9.txt", "task: t1\n\n")
        self.assertIn("only the pairing echo", msg)


class SkillContractTests(unittest.TestCase):
    """SKILL.md is the follower's executable contract, so a sentence in it is
    as load-bearing as a line of code."""

    def _skill(self):
        return (REPO / "skills" / "proactive-loop-pool" / "SKILL.md").read_text()

    def test_documented_acquire_resolves_workspace_rather_than_inheriting_it(self):
        # pool-core-wrapper.sh hands the child only CLAUDE_CONFIG_DIR,
        # SUTANDO_CORE_ID and SUTANDO_CORE_POOL_SIZE.
        wrapper = (REPO / "scripts" / "pool-core-wrapper.sh").read_text()
        self.assertNotIn("WORKSPACE=", wrapper.split("--add-dir")[0],
                         "wrapper now exports a workspace var; revisit SKILL.md")
        skill = self._skill()
        preamble = skill[:skill.index("pool_follower.py acquire")]
        self.assertIn('WORKSPACE="$(bash scripts/sutando-config.sh workspace)"',
                      preamble[-300:],
                      'unset $WORKSPACE expands empty -> acquire "/tasks" -> exit 2')

    def test_finish_writes_the_result_before_the_done_flag(self):
        # Anchors the doc claim to observed behaviour: inject a failure at the
        # done-flag write and the result is already on disk.
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            tasks = ws / "tasks"
            results = ws / "results"
            state = ws / "state"
            tasks.mkdir()
            results.mkdir()
            state.mkdir()
            claimed = tasks / "task-t1.claimed-core-9.txt"
            claimed.write_text("id: task-t1\n")
            state.chmod(0o500)
            try:
                with self.assertRaises(OSError):
                    pf.finish_task(tasks, results, state, "core-9",
                                   claimed, "task: t1\nthe answer\n")
                self.assertTrue((results / "task-t1.txt").is_file(),
                                "result absent -> the done flag came first")
            finally:
                state.chmod(0o700)


class LeadLabelTests(unittest.TestCase):
    def test_daemon_imports_the_label_rather_than_redefining_it(self):
        src = (REPO / "scripts" / "pool-lead-daemon.py").read_text()
        self.assertIn("LEAD_LABEL", src)
        self.assertNotIn('LEAD_LABEL = "', src,
                         "second definition drifts from pool_follower's")


if __name__ == "__main__":
    unittest.main(verbosity=2)
