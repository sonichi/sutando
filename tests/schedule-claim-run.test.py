#!/usr/bin/env python3
"""A schedule run has one record per (schedule id, slot): claimed -> waiting_approval -> done.

Pinned here: the key and tier come only from attested headers (agent_trigger_id of a
schedule.fired task, access_tier; missing or unknown is guest), never the body, and each fire
carries its own tier; a retried fire of the same slot skips even under a new task id; a new
slot runs; concurrent claimants of one slot produce exactly one winner; only the claim's run id
and current attempt can wait, resume or close a run, and resume starts a new attempt, so a
delayed transition from an earlier attempt and a redelivered fire are both refused; settlement
reads the current attempt's own result, under a named gateway's broker id, never an earlier
waiting notice; the script loads without fcntl; a stale
claim settles from the outbox's own outcome (CONFIRMED is done; NOT_DELIVERED, OUTCOME_UNKNOWN or
no item is needs_reconcile, never re-run); and
SKILL.md runs the claim before the job.

Run: python3 tests/schedule-claim-run.test.py
"""
import importlib.util
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/schedule/scripts/claim_run.py"
SKILL = REPO / "skills/schedule/SKILL.md"

sys.path.insert(0, str(REPO / "src"))
import outbox  # noqa: E402
import task_envelope  # noqa: E402

# The CLI runs under coverage when the gate asks, so the measured lines include main().
PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]

_spec = importlib.util.spec_from_file_location("claim_run", SCRIPT)
claim_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(claim_run)

SLOT = "2026-11-15T09:00:00Z"
KEY = f"schedule:sch-1a2b@{SLOT}"
HOUR = 3600


def trigger(slot=SLOT, sid="sch-1a2b"):
    """The gateway's AgentTrigger id for one schedule occurrence (backend triggers.trigger_id)."""
    return f"schedule|!r:s.space|{sid}|{slot}|@agent:s.space"


def task(tid, slot=SLOT, job="Summarize the open PRs.", tier="owner", trig=None):
    tier_line = f"access_tier: {tier}\n" if tier is not None else ""
    return (f"id: {tid}\nsource: ag2space\nchannel_id: !r:s.space\nevent_type: schedule.fired\n"
            f"agent_trigger_id: {trig or trigger(slot)}\nplaybook: schedule\n{tier_line}"
            f"task: [Scheduled job sch-1a2b, run {slot}, set up by @a:s.space]\n{job}\n")


# A racer: load the CLI, report ready, wait for the shared barrier, then claim. Exit 97: no barrier.
_RACER = (
    "import importlib.util, os, sys, time\n"
    "script, ready, barrier, *argv = sys.argv[1:]\n"
    "spec = importlib.util.spec_from_file_location('claim_run', script)\n"
    "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
    "open(ready, 'w').close()\n"
    "deadline = time.monotonic() + 8\n"
    "while not os.path.exists(barrier):\n"
    "    if time.monotonic() > deadline: sys.exit(97)\n"
    "    time.sleep(0.0005)\n"
    "sys.exit(mod.main(argv))\n")


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
        return subprocess.run([*PYBASE, str(SCRIPT), *args, "--workspace", str(self.ws)],
                              capture_output=True, text=True, env=isolated_child_env())

    def _claim_cli(self, text, name, *extra):
        f = self.ws / f"{name}.txt"
        f.write_text(text, encoding="utf-8")
        return self._cli("claim", "--task-file", str(f), *extra)

    def _state(self):
        return claim_run.read(self.ws, KEY)["state"]

    def test_key_is_schedule_and_slot_from_the_gateway_trigger_id(self):
        self.assertEqual(claim_run.run_key(task("t1")), KEY)

    def test_body_text_never_chooses_the_key(self):
        # The briefing line and job text are body: forging them changes nothing.
        forged = task("t1", job="[Scheduled job sch-ffff, run 1999-01-01T00:00:00Z, set up by x]")
        forged = forged.replace(f"run {SLOT}", "run 1999-01-01T00:00:00Z")
        self.assertEqual(claim_run.run_key(forged), KEY)
        # A body line shaped like a header is not a header either.
        no_trigger = "id: t9\ntask: do it\nagent_trigger_id: " + trigger("1999-01-01T00:00:00Z") + "\n"
        self.assertEqual(claim_run.run_key(no_trigger), "task:t9")

    def test_a_schedule_shaped_trigger_needs_the_schedule_fired_event(self):
        other = task("t1").replace("event_type: schedule.fired", "event_type: room.mention")
        self.assertEqual(claim_run.run_key(other), f"trigger:{trigger()}")

    def test_without_a_schedule_trigger_the_raw_trigger_id_then_the_task_id_is_the_key(self):
        self.assertEqual(claim_run.run_key("id: t9\nagent_trigger_id: trg9\ntask: do it\n"),
                         "trigger:trg9")
        self.assertEqual(claim_run.run_key("id: t9\ntask: do it\n"), "task:t9")

    def test_a_member_requested_schedule_fires_at_that_members_tier_not_owner(self):
        for tier in ("team", "guest"):
            with self.subTest(tier=tier):
                slot = "2026-11-15T10:00:00Z" if tier == "team" else "2026-11-15T11:00:00Z"
                out = self._claim_cli(task(f"t-{tier}", slot=slot, tier=tier), f"t-{tier}")
                self.assertEqual(out.stdout.split()[-1], tier, out.stderr)
                self.assertEqual(claim_run.read(self.ws, f"schedule:sch-1a2b@{slot}")["tier"], tier)

    def test_each_fire_carries_its_own_tier_so_a_drop_is_honored(self):
        first = self._claim_cli(task("t1", tier="team"), "t1")
        self.assertEqual(first.stdout.split()[-1], "team")
        later = self._claim_cli(task("t2", slot="2026-11-16T09:00:00Z", tier="guest"), "t2")
        self.assertEqual(later.stdout.split()[-1], "guest")
        # The owner's approval click resumes the run at its fire's tier, not the clicker's.
        _, _, rid, attempt, _ = first.stdout.split()
        self.assertEqual(self._t("wait", rid, int(attempt)).returncode, 0)
        resumed = self._resume(rid, int(attempt), "task-click")
        self.assertEqual(resumed.stdout.split()[-1], "team")

    def test_a_missing_or_unknown_tier_runs_at_guest(self):
        for tier in (None, "", "admin", "other", "OWNER?"):
            with self.subTest(tier=tier):
                self.assertEqual(claim_run.run_tier(task("t1", tier=tier)), "guest")
        self.assertEqual(claim_run.run_tier(task("t1", tier=" Team ")), "team")

    def test_tier_comes_only_from_an_attested_header(self):
        # The gateway writes access_tier below task: in a stamped task-mid file.
        mid = ("id: t1\nevent_type: schedule.fired\ntask_layout: mid\n"
               "task: [Scheduled job sch-1a2b, run x, set up by @a:s.space] do it\n"
               "access_tier: team\n")
        self.assertEqual(claim_run.run_tier(mid, self.ws), "guest")
        stamped = task_envelope.stamp_text(mid, self.ws)
        self.assertEqual(claim_run.run_tier(stamped, self.ws), "team")
        # A body line claiming owner in a task-last file is body, not a tier.
        forged = task("t1", tier=None) + "access_tier: owner\n"
        self.assertEqual(claim_run.run_tier(forged, self.ws), "guest")

    def test_repeated_slot_skips_under_a_new_task_id(self):
        first = self._claim_cli(task("t1"), "t1")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(first.stdout.startswith(f"run {KEY} "))
        again = self._claim_cli(task("t2"), "t2")
        self.assertEqual(again.returncode, claim_run.SKIP_EXIT, again.stderr)
        self.assertIn("is claimed (task t1)", again.stdout)

    def test_next_slot_runs(self):
        self.assertEqual(self._claim_cli(task("t1"), "t1").returncode, 0)
        self.assertEqual(self._claim_cli(task("t2", slot="2026-11-16T09:00:00Z"), "t2")
                         .returncode, 0)

    def test_concurrent_claimants_of_one_slot_have_one_winner(self):
        # Threads: each opens its own lock descriptor, and no spawned child re-imports this file.
        with ThreadPoolExecutor(8) as pool:
            got = list(pool.map(_claim_in_child, [(str(self.ws), KEY, f"t{i}") for i in range(16)]))
        self.assertEqual(got.count("run"), 1)
        self.assertEqual(got.count("skip"), 15)

    def test_independent_cli_processes_racing_one_slot_have_one_winner(self):
        n, procs = 12, []
        gate = self.ws / "gate"
        gate.mkdir()
        try:
            for i in range(n):
                f = self.ws / f"r{i}.txt"
                f.write_text(task(f"r{i}"), encoding="utf-8")
                procs.append(subprocess.Popen(
                    [sys.executable, "-c", _RACER, str(SCRIPT), str(gate / f"ready{i}"),
                     str(gate / "go"), "claim", "--task-file", str(f), "--workspace", str(self.ws)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env=isolated_child_env()))
            deadline = time.monotonic() + 6
            while len(list(gate.glob("ready*"))) < n and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(list(gate.glob("ready*"))), n, "racers did not all load")
            (gate / "go").touch()
            codes, outs = [], []
            for p in procs:
                out, err = p.communicate(timeout=max(0.1, deadline + 3 - time.monotonic()))
                codes.append(p.returncode)
                outs.append(out + err)
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait()
        self.assertEqual(codes.count(0), 1, outs)
        self.assertEqual(codes.count(claim_run.SKIP_EXIT), n - 1, outs)

    def _run(self, tid="task-1", **kw):
        verdict, rec = claim_run.claim(self.ws, KEY, tid, **kw)
        self.assertEqual(verdict, "run")
        return rec["run_id"]

    def _outbox_item(self, tid, root_name=".outbox", **fields):
        outbox._write_item(self.ws / "results" / root_name, tid, {"item_id": tid, "attempts": 1, **fields})

    def _t(self, cmd, rid, attempt, *extra):
        return self._cli(cmd, "--key", KEY, "--run-id", rid, "--attempt", str(attempt), *extra)

    def _resume(self, rid, attempt, tid):
        f = self.ws / f"{tid}.txt"
        f.write_text(f"id: {tid}\nhitl_click: true\ntask: approve\n", encoding="utf-8")
        return self._t("resume", rid, attempt, "--task-file", str(f))

    def test_cli_prints_the_run_id_and_attempt_the_record_holds(self):
        out = self._claim_cli(task("t1"), "t1").stdout.split()
        self.assertEqual(out, ["run", KEY, claim_run.read(self.ws, KEY)["run_id"], "1", "owner"])

    def test_done_run_skips_every_later_fire(self):
        rid = self._run()
        self.assertEqual(self._t("done", rid, 1).returncode, 0)
        late = self._claim_cli(task("t2"), "t2", "--stale-minutes", "0")
        self.assertEqual(late.returncode, claim_run.SKIP_EXIT)
        self.assertEqual(self._state(), "done")

    def test_waiting_approval_is_resumed_only_by_its_own_run_id_and_attempt(self):
        rid = self._run()
        self.assertEqual(self._t("wait", rid, 1).returncode, 0)
        # Hours later the run is still waiting, not reconciled or done.
        self.assertEqual(claim_run.sweep(self.ws, now=time.time() + 5 * HOUR), [])
        self.assertEqual(self._state(), "waiting_approval")
        for other, attempt in (("", 1), ("not-the-run-id", 1), (rid, 2)):
            self.assertIsNone(claim_run.transition(self.ws, KEY, "resume", other, attempt,
                                                   task_id="task-9"))
        resumed = self._resume(rid, 1, "task-click")
        self.assertEqual(resumed.stdout.split(), ["resume", KEY, rid, "2", "guest"], resumed.stderr)
        rec = claim_run.read(self.ws, KEY)
        self.assertEqual((rec["state"], rec["attempt"], rec["task_id"]), ("claimed", 2, "task-click"))
        self.assertEqual(self._t("done", rid, 2).returncode, 0)

    def test_a_delayed_transition_from_an_earlier_attempt_is_refused(self):
        rid = self._run()
        self.assertEqual(self._t("wait", rid, 1).returncode, 0)
        self.assertEqual(self._resume(rid, 1, "task-click").returncode, 0)
        # The reviewer's control: a late wait, resume or done carrying attempt 1.
        for cmd in ("wait", "done", "release"):
            self.assertEqual(self._t(cmd, rid, 1).returncode, claim_run.REFUSED_EXIT, cmd)
        self.assertEqual(self._resume(rid, 1, "task-click2").returncode, claim_run.REFUSED_EXIT)
        rec = claim_run.read(self.ws, KEY)
        self.assertEqual((rec["state"], rec["attempt"]), ("claimed", 2))
        self.assertEqual(self._t("wait", rid, 2).returncode, 0)

    def test_resume_needs_a_task_other_than_the_waiting_one(self):
        rid = self._run()
        claim_run.transition(self.ws, KEY, "wait", rid, 1)
        self.assertIsNone(claim_run.transition(self.ws, KEY, "resume", rid, 1, task_id="task-1"))
        self.assertIsNone(claim_run.transition(self.ws, KEY, "resume", rid, 1))
        self.assertEqual(self._state(), "waiting_approval")

    def test_a_redelivered_fire_never_takes_over_a_waiting_run(self):
        rid = self._run()
        claim_run.transition(self.ws, KEY, "wait", rid, 1)
        again = self._claim_cli(task("t2"), "t2", "--stale-minutes", "0")
        self.assertEqual(again.returncode, claim_run.SKIP_EXIT)
        self.assertIn("is waiting_approval", again.stdout)
        rec = claim_run.read(self.ws, KEY)
        self.assertEqual((rec["state"], rec["task_id"], rec["run_id"], rec["attempt"]),
                         ("waiting_approval", "task-1", rid, 1))

    def test_illegal_transitions_are_refused(self):
        r0 = self._t("wait", "x", 1)
        self.assertEqual(r0.returncode, claim_run.REFUSED_EXIT, r0.stdout + r0.stderr)
        rid = self._run()
        self.assertEqual(self._resume(rid, 1, "task-click").returncode, claim_run.REFUSED_EXIT)
        self.assertEqual(self._t("release", rid, 1).returncode, claim_run.REFUSED_EXIT)
        self.assertIsNone(claim_run.transition(self.ws, KEY, "done", "other", 1))
        self.assertEqual(self._state(), "claimed")

    def test_an_earlier_attempts_delivered_waiting_notice_does_not_settle_the_resumed_run(self):
        rid = self._run(now=time.time() - 3 * HOUR)
        claim_run.transition(self.ws, KEY, "wait", rid, 1, now=time.time() - 3 * HOUR)
        self._outbox_item("task-1", status="DELIVERED", provider_ref="$notice")
        claim_run.transition(self.ws, KEY, "resume", rid, 1, task_id="task-click",
                             now=time.time() - 2 * HOUR)
        swept = claim_run.sweep(self.ws)
        rec = claim_run.read(self.ws, KEY)
        self.assertEqual((rec["state"], rec["delivery"]), ("needs_reconcile", "none"))
        self.assertEqual([r["key"] for r in swept], [KEY])

    def test_the_resumed_attempts_own_confirmed_result_settles_it(self):
        rid = self._run(now=time.time() - 3 * HOUR)
        claim_run.transition(self.ws, KEY, "wait", rid, 1, now=time.time() - 3 * HOUR)
        claim_run.transition(self.ws, KEY, "resume", rid, 1, task_id="task-click",
                             now=time.time() - 2 * HOUR)
        self._outbox_item("task-click", status="DELIVERED", provider_ref="$final")
        self.assertEqual(claim_run.sweep(self.ws), [])
        self.assertEqual(self._state(), "done")

    def test_named_gateway_result_is_read_under_its_broker_id(self):
        sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
        from ag2_sparrow import gateway_instance
        local = gateway_instance.local_tid("b-77", "dev")
        self._outbox_item("b-77", root_name=".outbox.dev", status="DELIVERED", provider_ref="$e")
        # The primary outbox never answers for another instance's task, even under the local id.
        self._outbox_item(local, status="QUEUED")
        self.assertEqual(claim_run.delivery_outcome(self.ws, local), "CONFIRMED")
        self.assertIsNone(claim_run.delivery_outcome(self.ws, "b-77"))
        claim_run.claim(self.ws, KEY, local, now=time.time() - 2 * HOUR)
        self.assertEqual(claim_run.sweep(self.ws), [])
        self.assertEqual(self._state(), "done")

    def test_loads_without_fcntl(self):
        shim = ("import sys, types, importlib.abc, subprocess, ctypes.util, runpy\n"
                "class B(importlib.abc.MetaPathFinder):\n"
                "    def find_spec(self, n, p, t=None):\n"
                "        if n == 'fcntl': raise ModuleNotFoundError(name='fcntl')\n"
                "sys.modules.pop('fcntl', None); sys.meta_path.insert(0, B())\n"
                "sys.modules['msvcrt'] = types.ModuleType('msvcrt')\n"
                "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')\n")
        r = subprocess.run([sys.executable, "-c", shim, str(SCRIPT), "--help"],
                           capture_output=True, text=True, env=isolated_child_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("import fcntl", SCRIPT.read_text(encoding="utf-8"))

    def test_stale_claim_with_no_outbox_item_needs_reconcile_and_is_not_rerun(self):
        self._run(now=time.time() - 2 * HOUR)
        fire = self._claim_cli(task("t2"), "t2")
        self.assertEqual(fire.returncode, claim_run.RECONCILE_EXIT, fire.stderr)
        self.assertIn(f"needs-reconcile {KEY}", fire.stdout)
        self.assertIn("delivery none", fire.stdout)
        self.assertEqual(self._state(), "needs_reconcile")
        # Reported once: the next fire skips instead of asking the owner again.
        self.assertEqual(self._claim_cli(task("t3"), "t3").returncode, claim_run.SKIP_EXIT)

    def test_stale_claim_settles_from_the_outbox_outcome(self):
        cases = [({"status": "DELIVERED", "provider_ref": "$e"}, "done", "CONFIRMED"),
                 ({"status": "QUEUED"}, "needs_reconcile", "NOT_DELIVERED"),
                 ({"status": "CLAIMED", "dispatching": {"incarnation": "i", "at": 1}},
                  "needs_reconcile", "OUTCOME_UNKNOWN"),
                 ({"status": "READY", "provider_ref": "$e"}, "needs_reconcile", "OUTCOME_UNKNOWN"),
                 ({"status": "PARKED", "reason": "unknown"}, "needs_reconcile", "OUTCOME_UNKNOWN")]
        for i, (item, state, delivery) in enumerate(cases):
            with self.subTest(item=item):
                key, tid = f"schedule:sch-1a2b@slot{i}", f"task-{i}"
                claim_run.claim(self.ws, key, tid, now=time.time() - 2 * HOUR)
                self._outbox_item(tid, **item)
                swept = claim_run.sweep(self.ws)
                rec = claim_run.read(self.ws, key)
                self.assertEqual((rec["state"], rec["delivery"]), (state, delivery))
                self.assertEqual([r["key"] for r in swept], [key] if state != "done" else [])

    def test_unreadable_outbox_item_is_unknown_not_confirmed(self):
        self._run(now=time.time() - 2 * HOUR)
        root = self.ws / "results" / ".outbox"
        self._outbox_item("task-1", status="DELIVERED")
        outbox._item_path(root, "task-1").write_text("{not json", encoding="utf-8")
        self.assertEqual(claim_run.delivery_outcome(self.ws, "task-1"), "OUTCOME_UNKNOWN")

    def test_sweep_reports_stale_claims_once_and_leaves_fresh_ones(self):
        self._run(now=time.time() - 2 * HOUR)
        claim_run.claim(self.ws, "schedule:sch-1a2b@fresh", "t5")
        swept = self._cli("sweep").stdout.strip().splitlines()
        self.assertEqual(len(swept), 1)
        self.assertTrue(swept[0].startswith(f"needs-reconcile {KEY} "))
        self.assertEqual(self._cli("sweep").stdout.strip(), "")
        self.assertEqual(claim_run.read(self.ws, "schedule:sch-1a2b@fresh")["state"], "claimed")

    def test_owner_reconciles_with_done_or_release(self):
        rid = self._run(now=time.time() - 2 * HOUR)
        claim_run.sweep(self.ws)
        self.assertEqual(self._t("release", "x", 1).returncode, claim_run.REFUSED_EXIT)
        self.assertEqual(self._t("release", rid, 1).returncode, 0)
        self.assertIsNone(claim_run.read(self.ws, KEY))
        self.assertEqual(self._claim_cli(task("t2"), "t2").returncode, 0)

    def test_unreadable_record_reads_as_needs_reconcile_and_refuses_transitions(self):
        rid = self._run()
        claim_run._record_path(self.ws, KEY).write_text("{torn", encoding="utf-8")
        self.assertEqual(claim_run.read(self.ws, KEY)["state"], "needs_reconcile")
        self.assertEqual(self._t("done", rid, 1).returncode, claim_run.REFUSED_EXIT)
        again = self._claim_cli(task("t2"), "t2")
        self.assertEqual(again.returncode, claim_run.SKIP_EXIT)
        self.assertIn("is needs_reconcile", again.stdout)

    def test_sweep_skips_an_unreadable_record(self):
        self._run(now=time.time() - 2 * HOUR)
        (claim_run.claims_dir(self.ws) / "zz-torn.json").write_text("{torn", encoding="utf-8")
        swept = self._cli("sweep")
        self.assertEqual(swept.returncode, 0, swept.stderr)
        self.assertEqual(len(swept.stdout.strip().splitlines()), 1)

    def test_a_stale_fire_whose_run_was_confirmed_skips_and_settles_done(self):
        self._run(now=time.time() - 2 * HOUR)
        self._outbox_item("task-1", status="DELIVERED", provider_ref="$e")
        fire = self._claim_cli(task("t2"), "t2")
        self.assertEqual(fire.returncode, claim_run.SKIP_EXIT, fire.stderr)
        self.assertIn("is done", fire.stdout)

    def test_transitions_out_of_their_states_are_refused(self):
        rid = self._run()
        self.assertEqual(self._t("done", rid, 1).returncode, 0)
        for cmd in ("wait", "done", "release"):
            r = self._t(cmd, rid, 1)
            self.assertEqual(r.returncode, claim_run.REFUSED_EXIT, cmd)
            self.assertIn("refused: " + KEY + " is done", r.stdout)
        self.assertIn("is unclaimed", self._cli("wait", "--key", "task:nope", "--run-id", "x",
                                                "--attempt", "1").stdout)

    def test_owner_done_closes_a_needs_reconcile_run(self):
        rid = self._run(now=time.time() - 2 * HOUR)
        claim_run.sweep(self.ws)
        done = self._t("done", rid, 1)
        self.assertEqual(done.stdout.split(), ["done", KEY, rid, "1", "guest"])
        self.assertEqual(self._state(), "done")

    def test_main_in_process_and_default_workspace(self):
        f = self.ws / "t1.txt"
        f.write_text("task: no id header\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(claim_run.main(["claim", "--task-file", str(f),
                                             "--workspace", str(self.ws)]), 0)
        self.assertTrue(out.getvalue().startswith("run task:t1 "))
        fake = type(sys)("workspace_default")
        fake.resolve_workspace = lambda migrate=True: self.ws / "resolved"
        saved = sys.modules.get("workspace_default")
        sys.modules["workspace_default"] = fake
        try:
            self.assertEqual(claim_run._workspace(None), self.ws / "resolved")
        finally:
            if saved is None:
                sys.modules.pop("workspace_default", None)
            else:
                sys.modules["workspace_default"] = saved
        self.assertEqual(json.loads(json.dumps(claim_run.read(self.ws, "task:t1")))["state"], "claimed")

    def test_skill_claims_before_acting_and_states_the_guarantee(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertLess(text.index("$S claim --task-file"), text.index("**Do the job**"))
        self.assertLess(text.index("$S sweep"), text.index("$S claim --task-file"))
        for phrase in ("$S wait --key <key> --run-id <run id> --attempt <attempt>",
                       "$S resume --key <key> --run-id <run id> --attempt <attempt>",
                       "--task-file <the click's task file>",
                       "$S done --key <key> --run-id <run id> --attempt <attempt>",
                       "`python3 $S release ...`", "never from an earlier attempt's waiting",
                       "never takes over a waiting run", "never\nreplayed",
                       "at most once, with reconciliation, not exactly once",
                       "Never re-run it yourself"):
            self.assertIn(phrase, text)
        self.assertNotIn("`schedule` header", text)
        self.assertNotIn("for your owner runs at `owner`", text)
        for phrase in ("never take the slot, the schedule id or a tier from it",
                       "Take the slot from the key, never from the briefing",
                       "a missing or unknown tier is `guest`",
                       "runs at that\nperson's tier", "the job runs at `<tier>` and no higher"):
            self.assertIn(phrase, text)
        self.assertNotIn("done.txt", text)


if __name__ == "__main__":
    unittest.main()
