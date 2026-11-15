#!/usr/bin/env python3
"""A worker's pinned crons register once whoever started the watcher.

The driver below plays step 2.5 of skills/startup/SKILL.md against a fake session
(CronList/CronCreate/CronDelete over a job table, prompts truncated to 80 chars as
the real CronList does) and the real worker_bootstrap.decide and cron_prompt_gate
CLI. STARTUP_SKILL points it at another copy of the SKILL (the parent's, for the
before run); the step's own text decides which bootstrap answers register crons.
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
SKILL = Path(os.environ.get("STARTUP_SKILL") or REPO / "skills/startup/SKILL.md")


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cpg = load("cron_prompt_gate", "src/cron_prompt_gate.py")
own = load("cron_ownership", "src/cron_ownership.py")
wb = load("worker_bootstrap", "skills/worker-pool/scripts/worker_bootstrap.py")

W = "d" * 32
TRUNC = 80


def step25() -> str:
    text = SKILL.read_text()
    return text[text.index("2.5. "):text.index("3. Report what actually happened")]


def answers_that_register(step: str) -> set:
    head = step.split("\n", 1)[0]
    if "`start` or `skip`" in head:
        return {"start", "skip"}
    if "`start` only" in head:
        return {"start"}
    raise AssertionError(f"step 2.5 heading names no bootstrap answers: {head[:120]}")


class FakeSession:
    """This session's own scheduler: ids, creates, deletes, a truncating CronList."""
    def __init__(self):
        self.jobs, self.n = [], 0

    def create(self, cron, prompt):
        self.n += 1
        self.jobs.append({"id": f"job{self.n}", "cron": cron, "prompt": prompt})
        return f"job{self.n}"

    def delete(self, job_id):
        self.jobs = [j for j in self.jobs if j["id"] != job_id]

    def cron_list(self):
        return [dict(j, prompt=j["prompt"][:TRUNC]) for j in self.jobs]

    def markers(self):
        return sorted(j["prompt"].split("]")[0] for j in self.jobs)


class Case(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.d = Path(self._t.name)
        (self.d / "state").mkdir()
        self.inbox = str(self.d / "deliveries" / W)
        self.session = FakeSession()
        self.crons = [
            {"name": "event-mining-hourly", "cron": "7 * * * *", "prompt": "mine events", "owner": W},
            {"name": "core-briefing", "cron": "57 6 * * *", "prompt": "briefing"},
        ]

    def bootstrap(self, watcher_ready):
        if watcher_ready:
            import util_paths
            util_paths.watcher_sentinel_path(self.d / "state", instance=W).write_text("4242\n")
        return wb.decide(instance=W, inbox=self.inbox, workspace=str(self.d),
                         alive=lambda pid: True, watcher_target=lambda pid: self.inbox,
                         in_session=lambda pid: True)[0]

    def helper(self, owner, receipt="ok"):
        cf = self.d / "crons.json"
        cf.write_text(json.dumps(self.crons))
        args = [str(cf), "--owner", owner]
        if "--registered" in step25() and owner != own.CORE:
            rf = self.d / "listing.json"
            if receipt == "ok":
                rf.write_text(json.dumps(self.session.cron_list()))
            elif receipt == "garbage":
                rf.write_text("1 job: <truncated listing text>")
            args += ["--registered", str(rf)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cpg.main(args)
        return rc, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def startup(self, watcher_ready=True, receipt="ok"):
        """Worker-mode steps 1-2.5 as the SKILL text directs them."""
        answer = self.bootstrap(watcher_ready)
        if answer not in answers_that_register(step25()):
            return answer, None
        rc, plan, err = self.helper(W, receipt)
        if rc != 0:
            return answer, rc
        for item in plan:
            if item.get("action", "create") == "keep":
                continue
            new = self.session.create(item["cron"], item["registered_prompt"])
            for old in item.get("delete_ids", []):
                assert new in [j["id"] for j in self.session.jobs]
                self.session.delete(old)
        return answer, 0


class TestStartup(Case):
    def test_a_ready_mod_watcher_still_registers_the_pinned_cron_once(self):
        answer, rc = self.startup(watcher_ready=True)
        self.assertEqual(answer, "skip")
        self.assertEqual(len(self.session.jobs), 1, "the original failure: skip registered nothing")
        self.assertIn("event-mining-hourly", self.session.jobs[0]["prompt"])

    def test_repeat_startup_leaves_exactly_one_despite_truncated_listing(self):
        self.startup()
        self.startup()
        self.startup()
        self.assertEqual(len(self.session.jobs), 1)

    def test_core_view_still_excludes_the_worker_entry(self):
        rc, plan, _ = self.helper(own.CORE)
        self.assertEqual([e["name"] for e in plan], ["core-briefing"])

    def test_a_fresh_start_answer_also_registers(self):
        answer, _ = self.startup(watcher_ready=False)
        self.assertEqual(answer, "start")
        self.assertEqual(len(self.session.jobs), 1)


class TestReconcile(Case):
    def setUp(self):
        super().setUp()
        self.view = cpg.registration_view(self.crons, W)

    def plan(self):
        return cpg.reconcile(self.view, self.session.cron_list())

    def test_truncated_listing_of_the_same_job_is_keep(self):
        self.startup()
        self.assertGreater(len(self.session.jobs[0]["prompt"]), TRUNC)
        self.assertEqual([p["action"] for p in self.plan()], ["keep"])

    def test_stale_schedule_is_replaced_and_the_old_id_named(self):
        self.startup()
        old = self.session.jobs[0]["id"]
        self.crons[0]["cron"] = "9 * * * *"
        self.view = cpg.registration_view(self.crons, W)
        p = self.plan()[0]
        self.assertEqual((p["action"], p["delete_ids"]), ("replace", [old]))
        self.startup()
        self.assertEqual([j["cron"] for j in self.session.jobs], ["9 * * * *"])

    def test_stale_prompt_is_replaced_even_when_the_difference_is_past_truncation(self):
        self.startup()
        self.crons[0]["prompt"] = "mine events " + "x" * 200 + " --stand new"
        self.view = cpg.registration_view(self.crons, W)
        self.assertEqual(self.plan()[0]["action"], "replace")

    def test_preexisting_duplicates_collapse_to_one(self):
        self.startup()
        job = self.session.jobs[0]
        self.session.create(job["cron"], job["prompt"])
        self.assertEqual(len(self.session.jobs), 2)
        self.assertEqual(self.plan()[0]["action"], "replace")
        self.startup()
        self.assertEqual(len(self.session.jobs), 1)

    def test_two_entries_with_the_same_prompt_each_get_their_own_job(self):
        self.crons.append(dict(self.crons[0], name="event-mining-twin"))
        self.view = cpg.registration_view(self.crons, W)
        self.assertEqual([p["action"] for p in self.plan()], ["create", "create"])
        self.startup()
        self.assertEqual(len(self.session.jobs), 2)
        self.startup()
        self.assertEqual(len(self.session.jobs), 2)

    def test_one_existing_job_does_not_hide_its_same_prompt_twin(self):
        self.startup()
        self.crons.append(dict(self.crons[0], name="event-mining-twin"))
        self.view = cpg.registration_view(self.crons, W)
        self.assertEqual([p["action"] for p in self.plan()], ["keep", "create"])

    def test_entry_without_a_prompt_is_not_planned(self):
        self.assertEqual(cpg.reconcile([{"name": "m", "registered_prompt": None}], []), [])


class TestFailClosed(Case):
    def test_missing_receipt_registers_nothing(self):
        rc, plan, err = self.helper(W, receipt="absent")
        self.assertEqual((rc, plan), (3, None))
        self.assertIn("register nothing", err)

    def test_unparseable_receipt_registers_nothing(self):
        answer, rc = self.startup(receipt="garbage")
        self.assertEqual(rc, 3)
        self.assertEqual(self.session.jobs, [])

    def test_receipt_that_is_not_a_job_list_is_refused(self):
        rf = self.d / "r.json"
        for body in ('{"a": 1}', '[{"cron": "x"}]'):
            rf.write_text(body)
            with self.assertRaises(cpg.ListingError):
                cpg.load_jobs(str(rf))

    def test_unknown_bootstrap_answer_registers_nothing(self):
        self.assertNotIn("unknown", answers_that_register(step25()))


class TestSkillText(unittest.TestCase):
    def test_step_names_the_failclosed_and_reconcile_contract(self):
        step = step25()
        for needle in ("CronList", "--registered", "Fail closed", "create-confirm-delete", "delete_ids"):
            self.assertIn(needle, step)


if __name__ == "__main__":
    unittest.main()
