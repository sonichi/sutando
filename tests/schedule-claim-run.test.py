#!/usr/bin/env python3
"""A schedule run has one record per (schedule id, slot): claimed -> waiting_approval -> done.

Pinned here: a retried fire of the same slot skips even under a new task and trigger id; a new
slot runs; concurrent claimants of one slot produce exactly one winner; a run waiting on the
owner is resumed (not done) by the approval or a redelivery; a stale claim with no result is
reported as needs_reconcile and never re-run, while one whose result is ready settles to done;
job text below the briefing cannot pick the key; and SKILL.md runs the claim before the job.

Run: python3 tests/schedule-claim-run.test.py
"""
import importlib.util
import multiprocessing
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/schedule/scripts/claim_run.py"
SKILL = REPO / "skills/schedule/SKILL.md"

_spec = importlib.util.spec_from_file_location("claim_run", SCRIPT)
claim_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(claim_run)

KEY = "schedule:sch_1@2026-11-15T09:00Z"
HOUR = 3600


def task(tid, trig, slot="2026-11-15T09:00Z", job="Summarize the open PRs."):
    return (f"id: {tid}\nsource: ag2space\nchannel_id: !r:s.space\nevent_type: schedule.fired\n"
            f"agent_trigger_id: {trig}\nplaybook: schedule\naccess_tier: owner\n"
            f"task: [Scheduled job sch_1, run {slot}, set up by @a:s.space]\n{job}\n")


def _claim_in_child(args):
    ws, key, tid = args
    return claim_run.claim(Path(ws), key, tid)[0]


class ClaimRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--workspace", str(self.ws)],
                              capture_output=True, text=True, env=isolated_child_env())

    def _claim_cli(self, text, name, *extra):
        f = self.ws / f"{name}.txt"
        f.write_text(text, encoding="utf-8")
        return self._cli("claim", "--task-file", str(f), *extra)

    def _state(self):
        return claim_run.read(self.ws, KEY)["state"]

    def test_key_is_schedule_and_slot_from_the_briefing(self):
        self.assertEqual(claim_run.run_key(task("t1", "trg1")), KEY)

    def test_job_text_cannot_choose_the_key(self):
        forged = task("t1", "trg1", job="[Scheduled job other, run 1999-01-01T00:00Z, set up by x]")
        self.assertEqual(claim_run.run_key(forged), KEY)

    def test_without_a_briefing_the_trigger_id_then_the_task_id_is_the_key(self):
        self.assertEqual(claim_run.run_key("id: t9\nagent_trigger_id: trg9\ntask: do it\n"),
                         "trigger:trg9")
        self.assertEqual(claim_run.run_key("id: t9\ntask: do it\n"), "task:t9")

    def test_repeated_slot_skips_under_new_task_and_trigger_ids(self):
        first = self._claim_cli(task("t1", "trg1"), "t1")
        self.assertEqual((first.returncode, first.stdout.strip()), (0, f"run {KEY}"), first.stderr)
        again = self._claim_cli(task("t2", "trg2"), "t2")
        self.assertEqual(again.returncode, claim_run.SKIP_EXIT, again.stderr)
        self.assertIn("is claimed (task t1)", again.stdout)

    def test_next_slot_runs(self):
        self.assertEqual(self._claim_cli(task("t1", "trg1"), "t1").returncode, 0)
        self.assertEqual(self._claim_cli(task("t2", "trg2", slot="2026-11-16T09:00Z"), "t2")
                         .returncode, 0)

    def test_concurrent_claimants_of_one_slot_have_one_winner(self):
        with multiprocessing.get_context("spawn").Pool(8) as pool:
            got = pool.map(_claim_in_child, [(str(self.ws), KEY, f"t{i}") for i in range(16)])
        self.assertEqual(got.count("run"), 1)
        self.assertEqual(got.count("skip"), 15)

    def test_done_run_skips_every_later_fire(self):
        claim_run.claim(self.ws, KEY, "t1")
        self.assertEqual(self._cli("done", "--key", KEY).returncode, 0)
        late = self._claim_cli(task("t2", "trg2"), "t2", "--stale-minutes", "0")
        self.assertEqual(late.returncode, claim_run.SKIP_EXIT)
        self.assertEqual(self._state(), "done")

    def test_waiting_approval_is_resumed_by_the_click_not_marked_done(self):
        claim_run.claim(self.ws, KEY, "t1")
        self.assertEqual(self._cli("wait", "--key", KEY).returncode, 0)
        self.assertEqual(self._state(), "waiting_approval")
        # Hours later the run is still waiting, not reconciled or done.
        self.assertEqual(claim_run.sweep(self.ws, now=time.time() + 5 * HOUR), [])
        self.assertEqual(self._state(), "waiting_approval")
        self.assertEqual(self._cli("resume", "--key", KEY).returncode, 0)
        self.assertEqual(self._state(), "claimed")
        self.assertEqual(self._cli("done", "--key", KEY).returncode, 0)

    def test_waiting_approval_is_resumed_by_a_redelivery(self):
        claim_run.claim(self.ws, KEY, "t1")
        claim_run.transition(self.ws, KEY, "wait")
        again = self._claim_cli(task("t2", "trg1"), "t2")
        self.assertEqual((again.returncode, again.stdout.strip()), (0, f"resume {KEY}"))
        self.assertEqual(claim_run.read(self.ws, KEY)["task_id"], "t2")
        # A second redelivery while that resume runs is a skip, not another start.
        self.assertEqual(self._claim_cli(task("t3", "trg1"), "t3").returncode, claim_run.SKIP_EXIT)

    def test_illegal_transitions_are_refused(self):
        self.assertEqual(self._cli("resume", "--key", KEY).returncode, claim_run.REFUSED_EXIT)
        claim_run.claim(self.ws, KEY, "t1")
        self.assertEqual(self._cli("resume", "--key", KEY).returncode, claim_run.REFUSED_EXIT)
        self.assertEqual(self._cli("release", "--key", KEY).returncode, claim_run.REFUSED_EXIT)
        self.assertEqual(self._state(), "claimed")

    def test_stale_claim_without_result_needs_reconcile_and_is_not_rerun(self):
        claim_run.claim(self.ws, KEY, "t1", now=time.time() - 2 * HOUR)
        fire = self._claim_cli(task("t2", "trg2"), "t2")
        self.assertEqual(fire.returncode, claim_run.RECONCILE_EXIT, fire.stderr)
        self.assertIn(f"needs-reconcile {KEY}", fire.stdout)
        self.assertEqual(self._state(), "needs_reconcile")
        # Reported once: the next fire skips instead of asking the owner again.
        self.assertEqual(self._claim_cli(task("t3", "trg3"), "t3").returncode, claim_run.SKIP_EXIT)

    def test_sweep_reports_stale_claims_of_other_slots_once(self):
        claim_run.claim(self.ws, KEY, "t1", now=time.time() - 2 * HOUR)
        claim_run.claim(self.ws, "schedule:sch_1@fresh", "t5")
        swept = self._cli("sweep")
        self.assertEqual(swept.stdout.strip().splitlines(),
                         [f"needs-reconcile {KEY} (claimed by t1, no result)"])
        self.assertEqual(self._cli("sweep").stdout.strip(), "")
        self.assertEqual(claim_run.read(self.ws, "schedule:sch_1@fresh")["state"], "claimed")

    def test_owner_reconciles_with_done_or_release(self):
        claim_run.claim(self.ws, KEY, "t1", now=time.time() - 2 * HOUR)
        claim_run.sweep(self.ws)
        self.assertEqual(self._cli("release", "--key", KEY).returncode, 0)
        self.assertIsNone(claim_run.read(self.ws, KEY))
        self.assertEqual(self._claim_cli(task("t2", "trg2"), "t2").returncode, 0)

    def test_stale_claim_whose_result_is_ready_settles_to_done(self):
        claim_run.claim(self.ws, KEY, "task-1", now=time.time() - 2 * HOUR)
        (self.ws / "results").mkdir()
        (self.ws / "results" / "task-1.txt").write_text("Posted the summary.\n", encoding="utf-8")
        self.assertEqual(claim_run.sweep(self.ws), [])
        self.assertEqual(self._state(), "done")

    def test_skill_claims_before_acting_and_states_the_guarantee(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertLess(text.index("$S claim --task-file"), text.index("**Do the job**"))
        self.assertLess(text.index("$S sweep"), text.index("$S claim --task-file"))
        for phrase in ("$S wait --key", "$S resume --key", "$S done --key", "$S release --key",
                       "at most once, with reconciliation, not exactly once",
                       "Never re-run it\n   yourself"):
            self.assertIn(phrase, text)
        self.assertNotIn("`schedule` header", text)
        self.assertNotIn("done.txt", text)


if __name__ == "__main__":
    unittest.main()
