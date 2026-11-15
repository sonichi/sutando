#!/usr/bin/env python3
"""A worker's pinned crons register once whoever started the watcher.

The driver plays step 2.5 of skills/startup/SKILL.md against a fake session
(CronList/CronCreate/CronDelete over a job table, prompts truncated to 80 chars as
the real CronList does, ids unique per job). It runs the SKILL's own bash block, its
heredoc filled as the session would fill it, and its own `--record` command, plus
the real worker_bootstrap.decide. STARTUP_SKILL points it at another copy of the
SKILL (the parent's, for the before run); the step's own text decides which
bootstrap answers register crons.
"""
import contextlib
import importlib.util
import io
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
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
_ids = itertools.count(1)


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
    """One Claude session's scheduler: unique ids, creates, deletes, a truncating CronList."""
    def __init__(self):
        self.jobs = []
        self.fail_delete = set()   # ids whose CronDelete fails; "*" fails every delete
        self.deletes = []          # every delete attempted, in order

    def create(self, cron, prompt):
        job_id = f"job-{next(_ids)}"
        self.jobs.append({"id": job_id, "cron": cron, "prompt": prompt})
        return job_id

    def delete(self, job_id):
        self.deletes.append(job_id)
        if job_id in self.fail_delete or "*" in self.fail_delete:
            return False
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
        return True

    def cron_list(self):
        return [dict(j, prompt=j["prompt"][:TRUNC]) for j in self.jobs]


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

    @property
    def registry(self):
        return self.d / "state" / f"worker-crons-{W}.json"

    def bootstrap(self, watcher_ready):
        if watcher_ready:
            import util_paths
            util_paths.watcher_sentinel_path(self.d / "state", instance=W).write_text("4242\n")
        return wb.decide(instance=W, inbox=self.inbox, workspace=str(self.d),
                         alive=lambda pid: True, watcher_target=lambda pid: self.inbox,
                         in_session=lambda pid: True)[0]

    def host_dir(self):
        label = subprocess.run(["bash", str(REPO / "scripts/sutando-config.sh"), "host-label"],
                               capture_output=True, text=True, check=True).stdout.strip()
        d = self.d / "hosts" / label
        d.mkdir(parents=True, exist_ok=True)
        return d

    def env(self, owner):
        return dict(os.environ, SUTANDO_WATCHER_CMD=str(REPO / "src/watch-tasks-stream.sh"),
                    SUTANDO_WORKSPACE_DIR=str(self.d), SUTANDO_INSTANCE_ID=owner,
                    SUTANDO_PY=sys.executable, TMPDIR=str(self.d))

    def block(self):
        return textwrap.dedent(re.search(r"```bash\n(.*?)```", step25(), re.S).group(1))

    def helper(self, owner, jobs=None):
        """Run the SKILL's own bash block; `jobs` is the text pasted into its heredoc."""
        (self.host_dir() / "crons.json").write_text(json.dumps(self.crons))
        block = self.block()
        if "<<'JSON'" in block:
            body = json.dumps(self.session.cron_list()) if jobs is None else jobs
            block = re.sub(r"(<<'JSON'\n).*?(\nJSON)", lambda m: m.group(1) + body + m.group(2),
                           block, flags=re.S)
        r = subprocess.run(["bash", "-c", block], capture_output=True, text=True, env=self.env(owner))
        return r.returncode, (json.loads(r.stdout) if r.returncode == 0 else None), r.stderr

    def step_cmd(self, flag, name, job_id):
        """One of the step's own helper commands, with its variables defined as the block defines them."""
        m = re.search(r"`(\"\$SUTANDO_PY\"[^`]*" + re.escape(flag) + r" <name> <[a-z ]*id>)`", step25())
        if not m:
            return None
        defs = "\n".join(ln for ln in self.block().splitlines() if re.match(r"(REPO|H|CF|REG)=", ln))
        cmd = re.sub(r"<name>", name, m.group(1))
        cmd = re.sub(r"<[a-z ]*id>", job_id, cmd)
        return subprocess.run(["bash", "-c", defs + "\n" + cmd], capture_output=True, text=True,
                              env=self.env(W))

    def record(self, name, job_id):
        return self.step_cmd("--record", name, job_id)

    def confirm_deleted(self, name, job_id):
        return self.step_cmd("--confirm-deleted", name, job_id)

    def startup(self, watcher_ready=True, jobs=None, crash_after_record=False):
        """Worker-mode steps 1-2.5 as the SKILL text directs them."""
        answer = self.bootstrap(watcher_ready)
        if answer not in answers_that_register(step25()):
            return answer, None
        rc, plan, err = self.helper(W, jobs)
        if rc != 0:
            return answer, rc
        for item in plan:
            name = item["name"]
            if item.get("action", "create") != "keep":
                new = self.session.create(item["cron"], item["registered_prompt"])
                self.record(name, new)
            if crash_after_record:
                continue
            for old in item.get("delete_ids", []):
                self.session.delete(old)
                if old not in [j["id"] for j in self.session.jobs]:
                    self.confirm_deleted(name, old)
            for gone in item.get("clear_ids", []):
                self.confirm_deleted(name, gone)
        return answer, 0

    def view(self):
        return cpg.registration_view(self.crons, W, str(self.d))

    def plan(self):
        reg = cpg.load_registry(str(self.registry))
        return cpg.plan_cron_actions(self.view(), self.session.cron_list(), reg)


class TestStartup(Case):
    def test_a_ready_mod_watcher_still_registers_the_pinned_cron_once(self):
        answer, rc = self.startup(watcher_ready=True)
        self.assertEqual(answer, "skip")
        self.assertEqual(len(self.session.jobs), 1, "the original failure: skip registered nothing")
        self.assertIn("mine events", self.session.jobs[0]["prompt"])

    def test_repeat_startup_leaves_exactly_one(self):
        for _ in range(3):
            self.startup()
        self.assertEqual(len(self.session.jobs), 1)

    def test_core_view_still_excludes_the_worker_entry(self):
        rc, plan, _ = self.helper(own.CORE)
        self.assertEqual(rc, 0)
        self.assertEqual([e["name"] for e in plan], ["core-briefing"])

    def test_a_fresh_start_answer_also_registers(self):
        answer, _ = self.startup(watcher_ready=False)
        self.assertEqual(answer, "start")
        self.assertEqual(len(self.session.jobs), 1)


class TestDispatchedPrompt(Case):
    def test_an_argument_free_skill_entry_registers_exactly_its_configured_prompt(self):
        self.crons = [{"name": "dream", "cron": "37 3 * * *", "prompt_skill": "obsidian-dream", "owner": W}]
        self.startup()
        self.assertEqual([j["prompt"] for j in self.session.jobs], ["/obsidian-dream"])

    def test_a_plain_prompt_is_registered_verbatim_too(self):
        self.crons = [{"name": "daily", "cron": "5 6 * * *", "prompt": "Summarize the inbox", "owner": W}]
        self.startup()
        self.assertEqual([j["prompt"] for j in self.session.jobs], ["Summarize the inbox"])

    def test_long_skill_names_plan_nothing_on_repeat(self):
        for n in (30, 55, 56, 70, 120):
            with self.subTest(skill_name_length=n):
                self.setUp()
                self.crons = [{"name": "dream", "cron": "37 3 * * *", "prompt_skill": "s" * n, "owner": W}]
                self.startup()
                first = list(self.session.jobs)
                self.assertEqual(first[0]["prompt"], "/" + "s" * n)
                self.assertEqual([p["action"] for p in self.plan()], ["keep"])
                self.startup()
                self.assertEqual(self.session.jobs, first)

    def test_long_entry_names_plan_nothing_on_repeat(self):
        for n in (56, 66, 70, 120):
            with self.subTest(name_length=n):
                self.setUp()
                self.crons = [{"name": "n" * n, "cron": "7 * * * *", "prompt": "mine events", "owner": W}]
                self.startup()
                first = list(self.session.jobs)
                self.startup()
                self.assertEqual(self.session.jobs, first)

    def test_names_a_and_a_colon_b_stay_independent(self):
        self.crons = [{"name": "a", "cron": "7 * * * *", "prompt": "one", "owner": W},
                      {"name": "a:b", "cron": "8 * * * *", "prompt": "two", "owner": W}]
        self.startup()
        self.crons[0]["cron"] = "9 * * * *"
        plan = {p["name"]: p for p in self.plan()}
        by_id = {j["id"]: j for j in self.session.jobs}
        self.assertEqual(plan["a"]["action"], "replace")
        self.assertEqual([by_id[i]["cron"] for i in plan["a"]["delete_ids"]], ["7 * * * *"])
        self.assertEqual(plan["a:b"]["action"], "keep")


class TestRegistry(Case):
    def test_registry_records_the_job_id_and_survives_a_restart_of_the_helper(self):
        self.startup()
        data = json.loads(self.registry.read_text())
        (job,) = self.session.jobs
        self.assertEqual(data["entries"]["event-mining-hourly"]["id"], job["id"])
        rc, plan, _ = self.helper(W)
        self.assertEqual([p["action"] for p in plan], ["keep"])

    def test_id_missing_from_the_listing_recreates_exactly_once(self):
        self.startup()
        self.session = FakeSession()
        self.startup()
        self.assertEqual(len(self.session.jobs), 1)
        self.startup()
        self.assertEqual(len(self.session.jobs), 1)

    def test_job_deleted_by_hand_is_recreated_once(self):
        self.startup()
        self.session.delete(self.session.jobs[0]["id"])
        self.startup()
        self.startup()
        self.assertEqual(len(self.session.jobs), 1)

    def test_changed_schedule_or_prompt_is_replaced_create_confirm_delete(self):
        self.startup()
        old = self.session.jobs[0]["id"]
        self.crons[0]["cron"] = "9 * * * *"
        p = self.plan()[0]
        self.assertEqual((p["action"], p["delete_ids"]), ("replace", [old]))
        self.startup()
        self.assertEqual([j["cron"] for j in self.session.jobs], ["9 * * * *"])
        self.crons[0]["prompt"] = "mine events " + "x" * 200 + " --stand new"
        self.assertEqual(self.plan()[0]["action"], "replace")
        self.startup()
        self.assertEqual(len(self.session.jobs), 1)
        self.assertTrue(self.session.jobs[0]["prompt"].endswith("--stand new"))

    def test_same_prompt_entries_are_independent(self):
        self.crons.append(dict(self.crons[0], name="event-mining-twin"))
        self.startup()
        self.startup()
        self.assertEqual(len(self.session.jobs), 2)

    def test_a_job_with_no_record_is_unknowable_and_one_duplicate_is_planned(self):
        self.session.create("7 * * * *", "mine events")
        self.assertEqual(self.plan()[0]["action"], "create")

    def test_registry_is_per_owner(self):
        self.startup()
        reg = cpg.load_registry(str(self.registry))
        self.assertEqual(cpg.plan_cron_actions(self.view(), self.session.cron_list(), {})[0]["action"], "create")
        self.assertIn("event-mining-hourly", reg)

    def test_record_refuses_an_unknown_name_or_blank_id(self):
        r = self.record("no-such-entry", "job-1")
        self.assertEqual(r.returncode, 3)
        (self.host_dir() / "crons.json").write_text(json.dumps(self.crons))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cpg.main([str(self.host_dir() / "crons.json"), "--owner", W, "--registry",
                           str(self.registry), "--record", "event-mining-hourly", "  "])
        self.assertEqual(rc, 3)
        self.assertFalse(self.registry.exists())


class TestReplacementCleanup(Case):
    """A replacement is create, record, delete: the old id must outlive a failed or skipped delete."""
    def change_cron(self, cron):
        self.crons[0]["cron"] = cron

    def ids(self):
        return [j["id"] for j in self.session.jobs]

    def test_a_failed_delete_is_retried_alone_on_the_next_startup(self):
        self.startup()
        (old,) = self.ids()
        self.change_cron("9 * * * *")
        self.session.fail_delete = {old}
        self.startup()
        self.assertEqual(len(self.session.jobs), 2)
        self.session.fail_delete = set()
        self.session.deletes.clear()
        plan = self.plan()
        self.assertEqual([(p["action"], p["delete_ids"]) for p in plan], [("keep", [old])])
        self.startup()
        self.assertEqual(self.session.deletes, [old])
        self.assertEqual(len(self.session.jobs), 1)
        self.assertEqual(self.session.jobs[0]["cron"], "9 * * * *")

    def test_crash_after_record_before_delete_deletes_the_old_id_exactly_once(self):
        self.startup()
        (old,) = self.ids()
        self.change_cron("9 * * * *")
        self.startup(crash_after_record=True)
        self.assertEqual(len(self.session.jobs), 2)
        self.session.deletes.clear()
        self.startup()
        self.startup()
        self.assertEqual(self.session.deletes, [old])
        self.assertEqual(len(self.session.jobs), 1)

    def test_a_confirmed_delete_clears_the_pending_id_and_the_next_plan_is_keep(self):
        self.startup()
        (old,) = self.ids()
        self.change_cron("9 * * * *")
        self.startup()
        self.assertEqual(cpg.load_registry(str(self.registry))["event-mining-hourly"]["pending"], [])
        plan = self.plan()
        self.assertEqual([(p["action"], p["delete_ids"], p["clear_ids"]) for p in plan], [("keep", [], [])])
        self.assertNotIn(old, self.ids())

    def test_two_replacements_while_a_delete_keeps_failing_lose_neither_pending_id(self):
        self.startup()
        (first,) = self.ids()
        self.session.fail_delete = {"*"}
        self.change_cron("9 * * * *")
        self.startup()
        second = [i for i in self.ids() if i != first][0]
        self.change_cron("11 * * * *")
        self.startup()
        self.assertEqual(len(self.session.jobs), 3)
        pending = cpg.load_registry(str(self.registry))["event-mining-hourly"]["pending"]
        self.assertEqual(sorted(pending), sorted([first, second]))
        self.session.fail_delete = set()
        self.startup()
        self.assertEqual(len(self.session.jobs), 1)
        self.assertEqual(self.session.jobs[0]["cron"], "11 * * * *")
        self.assertEqual(cpg.load_registry(str(self.registry))["event-mining-hourly"]["pending"], [])

    def test_a_pending_id_already_gone_is_cleared_not_deleted(self):
        self.startup()
        (old,) = self.ids()
        self.change_cron("9 * * * *")
        self.session.fail_delete = {old}
        self.startup()
        # the job vanishes on its own (session expiry or a manual delete)
        self.session.jobs = [j for j in self.session.jobs if j["id"] != old]
        self.session.deletes.clear()
        (p,) = self.plan()
        self.assertEqual((p["action"], p["delete_ids"], p["clear_ids"]), ("keep", [], [old]))
        self.startup()
        self.assertEqual(self.session.deletes, [])
        self.assertEqual(cpg.load_registry(str(self.registry))["event-mining-hourly"]["pending"], [])

    def test_a_gone_recorded_job_still_deletes_a_pending_one_still_listed(self):
        self.startup()
        (old,) = self.ids()
        self.change_cron("9 * * * *")
        self.session.fail_delete = {old}
        self.startup()
        new = [i for i in self.ids() if i != old][0]
        self.session.jobs = [j for j in self.session.jobs if j["id"] != new]
        (p,) = self.plan()
        self.assertEqual((p["action"], p["delete_ids"]), ("create", [old]))

    def test_confirm_deleted_refuses_an_unknown_entry_and_ignores_an_unknown_id(self):
        self.startup()
        reg = str(self.registry)
        self.assertEqual(self.confirm_deleted("no-such-entry", "x").returncode, 3)
        before = self.registry.read_text()
        self.assertEqual(self.confirm_deleted("event-mining-hourly", "not-pending").returncode, 0)
        self.assertEqual(self.registry.read_text(), before)
        self.assertEqual(cpg.load_registry(reg)["event-mining-hourly"]["pending"], [])

    def test_a_corrupt_pending_field_registers_nothing(self):
        self.startup()
        data = json.loads(self.registry.read_text())
        data["entries"]["event-mining-hourly"]["pending"] = "oops"
        self.registry.write_text(json.dumps(data))
        rc, plan, err = self.helper(W)
        self.assertEqual((rc, plan), (3, None))
        self.assertIn("corrupt", err)


class TestFailClosed(Case):
    def refused(self, body=None, expect="register nothing"):
        rc, plan, err = self.helper(W, jobs=body)
        self.assertEqual((rc, plan), (3, None), (body, err))
        self.assertIn(expect, err)
        self.assertEqual(self.session.jobs, [])

    def test_garbage_receipt_registers_nothing(self):
        self.refused("1 job: <truncated listing text>")
        answer, rc = self.startup(jobs="not json")
        self.assertEqual(rc, 3)
        self.assertEqual(self.session.jobs, [])

    def test_short_blank_or_wrong_typed_jobs_are_refused(self):
        for body in ('[{"id":"job1"}]', '[{"id":"j","cron":"x"}]', '[{"id":"j","prompt":"p"}]',
                     '[{"id":"","cron":"x","prompt":"p"}]', '[{"id":"j","cron":"7 * * * *","prompt":""}]',
                     '[{"id":"j","cron":"  ","prompt":"p"}]', '[{"id":"j","cron":"x","prompt":"   "}]',
                     '[{"id":" ","cron":"x","prompt":"p"}]', '[{"id":["j"],"cron":"x","prompt":"p"}]',
                     '[{"id":true,"cron":"x","prompt":"p"}]', '[{"id":"j","cron":"x","prompt":5}]',
                     '{"a": 1}', '["job"]'):
            self.refused(body)

    def test_corrupt_or_unreadable_registry_registers_nothing(self):
        for body in ("not json", "[]", '{"entries": []}', '{"entries": {"a": {"id": "x"}}}',
                     '{"entries": {"a": {"id": "", "hash": "h"}}}'):
            self.registry.write_text(body)
            self.refused(expect="corrupt")
        self.registry.unlink()
        self.registry.mkdir()
        self.refused(expect="not readable")

    def test_duplicate_pinned_names_register_nothing(self):
        self.crons = [{"name": "dup", "cron": "7 * * * *", "prompt": "one", "owner": W},
                      {"name": "dup", "cron": "8 * * * *", "prompt": "two", "owner": W}]
        self.refused(expect="duplicate pinned names")
        self.assertFalse(self.registry.exists())

    def test_duplicate_names_across_owners_are_not_a_conflict(self):
        self.crons = [{"name": "dup", "cron": "7 * * * *", "prompt": "one", "owner": W},
                      {"name": "dup", "cron": "8 * * * *", "prompt": "two", "owner": "e" * 32}]
        rc, plan, _ = self.helper(W)
        self.assertEqual((rc, [p["name"] for p in plan]), (0, ["dup"]))

    def test_unnamed_entry_is_refused(self):
        self.crons = [{"cron": "7 * * * *", "prompt": "a", "owner": W},
                      {"name": " ", "cron": "8 * * * *", "prompt": "b", "owner": W},
                      {"name": "ok", "cron": "9 * * * *", "prompt": "c", "owner": W}]
        rc, plan, err = self.helper(W)
        self.assertEqual([p["name"] for p in plan], ["ok"])
        self.assertIn("no name", err)

    def test_unknown_bootstrap_answer_registers_nothing(self):
        self.assertNotIn("unknown", answers_that_register(step25()))


class TestInProcess(Case):
    """The same refusals through direct calls, so the coverage gate sees them."""
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cpg.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def cfg(self):
        f = self.d / "c.json"
        f.write_text(json.dumps(self.crons))
        return str(f)

    def test_load_jobs(self):
        f = self.d / "r.json"
        f.write_text('[{"id": 7, "cron": "* * * * *", "prompt": "p"}]')
        self.assertEqual(len(cpg.load_jobs(str(f))), 1)
        for body in ('[{"id":"job1"}]', '{"a":1}', '[{"id":"j","cron":"x","prompt":""}]', 'nope'):
            f.write_text(body)
            with self.assertRaises(cpg.ListingError):
                cpg.load_jobs(str(f))
        for bad in ("", None, str(self.d / "absent")):
            with self.assertRaises(cpg.ListingError):
                cpg.load_jobs(bad)

    def test_load_registry_and_record(self):
        reg = self.d / "state" / "r.json"
        self.assertEqual(cpg.load_registry(str(reg)), {})
        entry = self.view()[0]
        cpg.record(str(reg), entry, entry["registered_prompt"], "job-9")
        got = cpg.load_registry(str(reg))
        self.assertEqual(got[entry["name"]]["id"], "job-9")
        for bad in ("", None):
            with self.assertRaises(cpg.ListingError):
                cpg.load_registry(bad)
        with self.assertRaises(cpg.ListingError):
            cpg.record(str(reg), entry, "p", "")

    def test_registry_corruption_duplicates_and_promptless_entries(self):
        reg = self.d / "state" / "r.json"
        for body in ("not json", "[]", '{"entries": {"a": {"id": ""}}}'):
            reg.write_text(body)
            with self.assertRaises(cpg.ListingError):
                cpg.load_registry(str(reg))
        reg.unlink()
        reg.mkdir()
        with self.assertRaises(cpg.ListingError):
            cpg.load_registry(str(reg))
        view = [{"name": "d", "registered_prompt": "p", "cron": "x"}] * 2
        with self.assertRaises(cpg.ListingError):
            cpg.plan_cron_actions(view, [], {})
        self.assertEqual(cpg.plan_cron_actions([{"name": "m", "registered_prompt": None}], [], {}), [])

    def test_record_moves_the_superseded_id_to_pending_and_confirm_clears_it(self):
        reg = str(self.d / "state" / "p.json")
        entry = self.view()[0]
        cpg.record(reg, entry, entry["registered_prompt"], "old")
        cpg.record(reg, entry, entry["registered_prompt"], "old")
        self.assertEqual(cpg.load_registry(reg)[entry["name"]]["pending"], [])
        cpg.record(reg, entry, entry["registered_prompt"], "new")
        cpg.record(reg, entry, entry["registered_prompt"], "newer")
        self.assertEqual(cpg.load_registry(reg)[entry["name"]]["pending"], ["old", "new"])
        cpg.confirm_deleted(reg, entry["name"], "old")
        cpg.confirm_deleted(reg, entry["name"], "never-pending")
        self.assertEqual(cpg.load_registry(reg)[entry["name"]]["pending"], ["new"])
        with self.assertRaises(cpg.ListingError):
            cpg.confirm_deleted(reg, "other", "x")

    def test_main_confirm_deleted(self):
        reg = str(self.d / "state" / "c.json")
        entry = self.view()[0]
        cpg.record(reg, entry, entry["registered_prompt"], "a")
        cpg.record(reg, entry, entry["registered_prompt"], "b")
        rc, out, err = self.run_main(self.cfg(), "--owner", W, "--registry", reg,
                                     "--confirm-deleted", entry["name"], "a")
        self.assertEqual(rc, 0)
        self.assertEqual(cpg.load_registry(reg)[entry["name"]]["pending"], [])
        rc, out, err = self.run_main(self.cfg(), "--owner", W, "--registry", reg,
                                     "--confirm-deleted", "nope", "a")
        self.assertEqual(rc, 3)

    def test_main_plan_record_and_refusals(self):
        rf = self.d / "listing.json"
        rf.write_text("[]")
        reg = str(self.d / "state" / "reg.json")
        rc, out, err = self.run_main(self.cfg(), "--owner", W, "--registered", str(rf), "--registry", reg)
        self.assertEqual((rc, [p["action"] for p in json.loads(out)]), (0, ["create"]))
        rc, out, err = self.run_main(self.cfg(), "--owner", W, "--registry", reg, "--record",
                                     "event-mining-hourly", "job-1")
        self.assertEqual(rc, 0)
        rf.write_text('[{"id": "job-1", "cron": "7 * * * *", "prompt": "x"}]')
        rc, out, err = self.run_main(self.cfg(), "--owner", W, "--registered", str(rf), "--registry", reg)
        self.assertEqual([p["action"] for p in json.loads(out)], ["keep"])
        rc, out, err = self.run_main(self.cfg(), "--owner", W, "--registered", "", "--registry", reg)
        self.assertEqual((rc, out), (3, ""))
        rc, out, err = self.run_main(self.cfg(), "--owner", W, "--registered", str(rf))
        self.assertEqual((rc, out), (3, ""))
        self.assertIn("registry path is empty", err)
        rc, out, err = self.run_main(self.cfg(), "--owner", W, "--registry", reg, "--record", "nope", "j")
        self.assertEqual(rc, 3)

    def test_main_non_list_config_and_missing_config_and_unnamed(self):
        f = self.d / "c.json"
        f.write_text('{"a": 1}')
        self.assertEqual(self.run_main(str(f))[0], 2)
        f.unlink()
        self.assertEqual(self.run_main(str(f))[0], 0)
        self.crons = [{"cron": "7 * * * *", "prompt": "a", "owner": W}]
        rc, out, err = self.run_main(self.cfg(), "--owner", W)
        self.assertIn("no name", err)

    def test_without_the_flags_the_core_output_is_unchanged(self):
        rc, out, _ = self.run_main(self.cfg())
        self.assertNotIn("action", json.loads(out)[0])


class TestSkillText(unittest.TestCase):
    def test_step_defines_the_receipt_registry_and_contract(self):
        step = step25()
        for needle in ('LISTING="$(mktemp', "<<'JSON'", '--registered "$LISTING"', '--registry "$REG"',
                       "--record <name> <new job id>", "Fail closed", "create-confirm-delete",
                       "delete_ids", "clear_ids", "--confirm-deleted <name> <deleted id>", "pending",
                       "unknowable", "exactly its configured"):
            self.assertIn(needle, step)


if __name__ == "__main__":
    unittest.main()
