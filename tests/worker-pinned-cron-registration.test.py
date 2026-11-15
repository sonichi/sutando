#!/usr/bin/env python3
"""A worker's pinned crons register once whoever started the watcher.

The driver plays step 2.5 of skills/startup/SKILL.md against a fake session
(CronList/CronCreate/CronDelete over a job table, prompts truncated to 80 chars as
the real CronList does). It runs the SKILL's own bash block, its heredoc filled as
the session would fill it, plus the real worker_bootstrap.decide. STARTUP_SKILL
points it at another copy of the SKILL (the parent's, for the before run); the
step's own text decides which bootstrap answers register crons.
"""
import contextlib
import importlib.util
import io
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

    def host_dir(self):
        label = subprocess.run(["bash", str(REPO / "scripts/sutando-config.sh"), "host-label"],
                               capture_output=True, text=True, check=True).stdout.strip()
        d = self.d / "hosts" / label
        d.mkdir(parents=True, exist_ok=True)
        return d

    def helper(self, owner, jobs=None):
        """Run the SKILL's own bash block; `jobs` is the text pasted into its heredoc."""
        (self.host_dir() / "crons.json").write_text(json.dumps(self.crons))
        block = re.search(r"```bash\n(.*?)```", step25(), re.S).group(1)
        block = textwrap.dedent(block)
        if "<<'JSON'" in block:
            body = json.dumps(self.session.cron_list()) if jobs is None else jobs
            block = re.sub(r"(<<'JSON'\n).*?(\nJSON)", lambda m: m.group(1) + body + m.group(2),
                           block, flags=re.S)
        env = dict(os.environ, SUTANDO_WATCHER_CMD=str(REPO / "src/watch-tasks-stream.sh"),
                   SUTANDO_WORKSPACE_DIR=str(self.d), SUTANDO_INSTANCE_ID=owner,
                   SUTANDO_PY=sys.executable, TMPDIR=str(self.d))
        r = subprocess.run(["bash", "-c", block], capture_output=True, text=True, env=env)
        return r.returncode, (json.loads(r.stdout) if r.returncode == 0 else None), r.stderr

    def startup(self, watcher_ready=True, jobs=None):
        """Worker-mode steps 1-2.5 as the SKILL text directs them."""
        answer = self.bootstrap(watcher_ready)
        if answer not in answers_that_register(step25()):
            return answer, None
        rc, plan, err = self.helper(W, jobs)
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

    def view(self):
        return cpg.registration_view(self.crons, W, str(self.d))

    def plan(self):
        return cpg.plan_cron_actions(self.view(), self.session.cron_list(), W)


class TestStartup(Case):
    def test_a_ready_mod_watcher_still_registers_the_pinned_cron_once(self):
        answer, rc = self.startup(watcher_ready=True)
        self.assertEqual(answer, "skip")
        self.assertEqual(len(self.session.jobs), 1, "the original failure: skip registered nothing")
        self.assertIn("mine events", self.session.jobs[0]["prompt"])

    def test_repeat_startup_leaves_exactly_one_despite_truncated_listing(self):
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


class TestIdentity(Case):
    def test_long_names_stay_recognised_under_the_truncated_listing(self):
        for n in (30, 56, 60, 65, 66, 70, 120):
            with self.subTest(name_length=n):
                self.session = FakeSession()
                self.crons = [{"name": "n" * n, "cron": "7 * * * *", "prompt": "mine events", "owner": W}]
                self.startup()
                first = list(self.session.jobs)
                self.assertEqual([p["action"] for p in self.plan()], ["keep"])
                self.startup()
                self.assertEqual(self.session.jobs, first)

    def test_marker_is_fixed_length_and_visible_in_the_truncated_listing(self):
        self.crons[0]["name"] = "x" * 70
        self.startup()
        (job,) = self.session.cron_list()
        self.assertEqual(len(cpg.marker(W, "x" * 70, "p")), 24)
        self.assertIsNotNone(cpg.MARKER.search(job["prompt"]), job["prompt"])

    def test_names_a_and_a_colon_b_do_not_collide(self):
        self.crons = [{"name": "a", "cron": "7 * * * *", "prompt": "one", "owner": W},
                      {"name": "a:b", "cron": "8 * * * *", "prompt": "two", "owner": W}]
        self.startup()
        self.assertEqual(len(self.session.jobs), 2)
        self.crons[0]["cron"] = "9 * * * *"
        plan = {p["name"]: p for p in self.plan()}
        by_id = {j["id"]: j for j in self.session.jobs}
        self.assertEqual(plan["a"]["action"], "replace")
        self.assertEqual(len(plan["a"]["delete_ids"]), 1)
        self.assertEqual(by_id[plan["a"]["delete_ids"][0]]["cron"], "7 * * * *")
        self.assertEqual(plan["a:b"]["action"], "keep")

    def test_owner_is_part_of_the_identity(self):
        self.startup()
        other = cpg.plan_cron_actions(self.view(), self.session.cron_list(), "e" * 32)
        self.assertEqual(other[0]["action"], "create")

    def test_skill_entry_keeps_its_leading_slash(self):
        self.crons = [{"name": "dream", "cron": "37 3 * * *", "prompt_skill": "obsidian-dream", "owner": W}]
        (item,) = self.plan()
        self.assertTrue(item["registered_prompt"].startswith("/obsidian-dream "), item)
        self.assertEqual(item["registered_prompt"].split()[1], cpg.marker(W, "dream", "/obsidian-dream"))
        self.startup()
        self.assertEqual([p["action"] for p in self.plan()], ["keep"])

    def test_unnamed_entry_is_refused_not_shared(self):
        self.crons = [{"cron": "7 * * * *", "prompt": "a", "owner": W},
                      {"name": "", "cron": "8 * * * *", "prompt": "b", "owner": W},
                      {"name": "ok", "cron": "9 * * * *", "prompt": "c", "owner": W}]
        rc, plan, err = self.helper(W)
        self.assertEqual([p["name"] for p in plan], ["ok"])
        self.assertIn("no name", err)


class TestReconcile(Case):
    def test_truncated_listing_of_the_same_job_is_keep(self):
        self.startup()
        self.assertEqual([p["action"] for p in self.plan()], ["keep"])

    def test_stale_schedule_is_replaced_and_the_old_id_named(self):
        self.startup()
        old = self.session.jobs[0]["id"]
        self.crons[0]["cron"] = "9 * * * *"
        p = self.plan()[0]
        self.assertEqual((p["action"], p["delete_ids"]), ("replace", [old]))
        self.startup()
        self.assertEqual([j["cron"] for j in self.session.jobs], ["9 * * * *"])

    def test_stale_prompt_is_replaced_even_when_the_difference_is_past_truncation(self):
        self.startup()
        self.crons[0]["prompt"] = "mine events " + "x" * 200 + " --stand new"
        self.assertEqual(self.plan()[0]["action"], "replace")

    def test_preexisting_duplicates_collapse_to_one(self):
        self.startup()
        job = self.session.jobs[0]
        self.session.create(job["cron"], job["prompt"])
        self.assertEqual(self.plan()[0]["action"], "replace")
        self.startup()
        self.assertEqual(len(self.session.jobs), 1)

    def test_two_entries_with_the_same_prompt_each_get_their_own_job(self):
        self.crons.append(dict(self.crons[0], name="event-mining-twin"))
        self.assertEqual([p["action"] for p in self.plan()], ["create", "create"])
        self.startup()
        self.startup()
        self.assertEqual(len(self.session.jobs), 2)

    def test_one_existing_job_does_not_hide_its_same_prompt_twin(self):
        self.startup()
        self.crons.append(dict(self.crons[0], name="event-mining-twin"))
        self.assertEqual([p["action"] for p in self.plan()], ["keep", "create"])

    def test_entry_without_a_prompt_is_not_planned(self):
        self.assertEqual(cpg.plan_cron_actions([{"name": "m", "registered_prompt": None}], [], W), [])

    def test_a_premarker_job_is_not_recognised_so_one_duplicate_is_planned(self):
        self.session.create("7 * * * *", "mine events")
        self.assertEqual(self.plan()[0]["action"], "create")


class TestFailClosed(Case):
    def refused(self, body):
        rc, plan, err = self.helper(W, jobs=body)
        self.assertEqual((rc, plan), (3, None), body)
        self.assertIn("register nothing", err)

    def test_garbage_receipt_registers_nothing(self):
        self.refused("1 job: <truncated listing text>")
        answer, rc = self.startup(jobs="not json")
        self.assertEqual(rc, 3)
        self.assertEqual(self.session.jobs, [])

    def test_short_or_wrong_typed_jobs_are_refused(self):
        for body in ('[{"id":"job1"}]', '[{"id":"j","cron":"x"}]', '[{"id":"j","prompt":"p"}]',
                     '[{"id":"","cron":"x","prompt":"p"}]', '[{"id":["j"],"cron":"x","prompt":"p"}]',
                     '[{"id":true,"cron":"x","prompt":"p"}]', '[{"id":"j","cron":"x","prompt":5}]',
                     '{"a": 1}', '["job"]'):
            self.refused(body)

    def test_cli_empty_path_is_not_absence(self):
        cf = self.d / "c.json"
        cf.write_text(json.dumps(self.crons))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cpg.main([str(cf), "--owner", W, "--registered", ""])
        self.assertEqual((rc, out.getvalue()), (3, ""))
        self.assertIn("empty", err.getvalue())

    def test_cli_missing_file_is_refused(self):
        cf = self.d / "c.json"
        cf.write_text(json.dumps(self.crons))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cpg.main([str(cf), "--owner", W, "--registered", str(self.d / "nope")]), 3)

    def test_without_the_flag_the_core_output_is_unchanged(self):
        cf = self.d / "c.json"
        cf.write_text(json.dumps(self.crons))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cpg.main([str(cf)])
        self.assertNotIn("action", json.loads(out.getvalue())[0])

    def test_unknown_bootstrap_answer_registers_nothing(self):
        self.assertNotIn("unknown", answers_that_register(step25()))


class TestInProcess(Case):
    """The same refusals through direct calls, so the coverage gate sees them."""
    def test_load_jobs_accepts_good_and_refuses_bad(self):
        f = self.d / "r.json"
        f.write_text('[{"id": 7, "cron": "* * * * *", "prompt": "p"}]')
        self.assertEqual(len(cpg.load_jobs(str(f))), 1)
        for body in ('[{"id":"job1"}]', '{"a":1}', '[{"id":true,"cron":"x","prompt":"p"}]',
                     '[{"id":"j","cron":"","prompt":"p"}]', 'nope'):
            f.write_text(body)
            with self.assertRaises(cpg.ListingError):
                cpg.load_jobs(str(f))
        for bad in ("", None, str(self.d / "absent")):
            with self.assertRaises(cpg.ListingError):
                cpg.load_jobs(bad)

    def test_main_refuses_unnamed_entry_and_non_list_config(self):
        cf, rf = self.d / "c.json", self.d / "r.json"
        rf.write_text("[]")
        cf.write_text(json.dumps([{"cron": "7 * * * *", "prompt": "a", "owner": W},
                                  {"name": "ok", "cron": "9 * * * *", "prompt": "c", "owner": W}]))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cpg.main([str(cf), "--owner", W, "--registered", str(rf)])
        self.assertEqual((rc, [p["name"] for p in json.loads(out.getvalue())]), (0, ["ok"]))
        self.assertIn("no name", err.getvalue())
        cf.write_text('{"a": 1}')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cpg.main([str(cf)]), 2)
            cf.unlink()
            self.assertEqual(cpg.main([str(cf)]), 0)


class TestSkillText(unittest.TestCase):
    def test_step_defines_the_receipt_and_the_contract(self):
        step = step25()
        for needle in ('LISTING="$(mktemp', "<<'JSON'", '--registered "$LISTING"', "Fail closed",
                       "create-confirm-delete", "delete_ids", "spans the upgrade"):
            self.assertIn(needle, step)


if __name__ == "__main__":
    unittest.main()
