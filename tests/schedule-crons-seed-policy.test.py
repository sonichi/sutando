#!/usr/bin/env python3
"""A missing per-host crons.json must never be self-healed into jobs the owner did not opt into.

Runs `/schedule-crons` step 1 as the agent would: the step's last inline code span, executed by
bash in a scratch checkout whose `scripts/sutando-config.sh` points at a scratch workspace. The
installer-managed in-checkout `skills/schedule-crons/crons.json` holds the shipped sample jobs.
`crons.example.pre-opt-in.json` is the template as shipped before `opt_in` existed.

Run: python3 tests/schedule-crons-seed-policy.test.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "schedule-crons"
OPT_IN = {"morning-briefing", "example-digest"}
PRE_OPT_IN = REPO / "tests" / "fixtures" / "schedule-crons" / "crons.example.pre-opt-in.json"


def step1_command() -> str:
    text = (SKILL / "SKILL.md").read_text()
    section = text.split("## On Activation", 1)[1]
    line = next(ln for ln in section.splitlines() if ln.startswith("1. "))
    return re.findall(r"`([^`]+)`", line)[-1]


class SeedPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        shutil.copytree(SKILL, self.repo / "skills" / "schedule-crons",
                        ignore=shutil.ignore_patterns("crons.json", "__pycache__"))
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "sutando-config.sh").write_text(
            f'case "$1" in workspace) echo "{self.ws}";; host-label) echo h1;; esac\n')
        self.example = json.loads((SKILL / "crons.example.json").read_text())
        self.legacy = self.repo / "skills" / "schedule-crons" / "crons.json"
        self.target = self.ws / "hosts" / "h1" / "crons.json"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_step1(self) -> str:
        out = subprocess.run(["bash", "-c", step1_command()], cwd=self.repo,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def names(self) -> set:
        return {e["name"] for e in json.loads(self.target.read_text())}

    def test_example_marks_the_opt_in_jobs(self):
        marked = {e["name"] for e in self.example if e.get("opt_in") is True}
        self.assertEqual(marked, OPT_IN)

    def test_legacy_sample_jobs_are_not_enabled(self):
        self.legacy.write_text(json.dumps(self.example))
        self.run_step1()
        self.assertIn("main-loop", self.names())
        self.assertFalse(self.names() & OPT_IN, self.names())

    def test_fresh_install_without_legacy_enables_no_opt_in_job(self):
        self.run_step1()
        self.assertIn("main-loop", self.names())
        self.assertFalse(self.names() & OPT_IN, self.names())

    def test_owner_legacy_jobs_are_kept_and_named_not_enabled(self):
        own = {"name": "my-report", "cron": "0 9 * * *", "prompt": "x"}
        self.legacy.write_text(json.dumps(self.example + [own]))
        out = self.run_step1()
        self.assertNotIn("my-report", self.names())
        self.assertIn("not-enabled my-report kept at", out)
        kept = json.loads((self.ws / "hosts" / "h1" / "crons.legacy.json").read_text())
        self.assertIn(own, kept)

    def test_customized_starter_name_job_is_kept_and_named(self):
        own = {"name": "main-loop", "cron": "0 9 * * *", "prompt": "my owner-customized task"}
        self.legacy.write_text(json.dumps([own]))
        out = self.run_step1()
        self.assertIn("not-enabled main-loop kept at", out)
        kept = json.loads((self.ws / "hosts" / "h1" / "crons.legacy.json").read_text())
        self.assertIn(own, kept)

    def test_pre_upgrade_shipped_template_is_not_called_owner_jobs(self):
        self.legacy.write_text(PRE_OPT_IN.read_text())
        out = self.run_step1()
        self.assertNotIn("not-enabled", out)
        self.assertFalse((self.ws / "hosts" / "h1" / "crons.legacy.json").exists())
        self.assertFalse(self.names() & OPT_IN, self.names())

    def test_first_install_only_seeds_just_the_named_starter_jobs(self):
        out = subprocess.run(
            ["python3", "skills/schedule-crons/scripts/seed_crons.py",
             "--first-install-only", "main-loop"],
            cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.names(), {"main-loop"})

    def test_first_install_only_leaves_an_interim_file_whole(self):
        (self.ws / "crons").mkdir()
        own = [{"name": "mine", "cron": "0 9 * * *", "prompt": "x"}]
        (self.ws / "crons" / "h1.json").write_text(json.dumps(own))
        out = subprocess.run(
            ["python3", "skills/schedule-crons/scripts/seed_crons.py",
             "--first-install-only", "main-loop"],
            cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(self.target.read_text()), own)

    def test_interim_workspace_file_is_migrated_whole(self):
        (self.ws / "crons").mkdir()
        own = [{"name": "mine", "cron": "0 9 * * *", "prompt": "x"}]
        (self.ws / "crons" / "h1.json").write_text(json.dumps(own))
        self.run_step1()
        self.assertEqual(json.loads(self.target.read_text()), own)

    def test_existing_file_is_untouched(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('[{"name":"mine"}]')
        self.legacy.write_text(json.dumps(self.example))
        self.run_step1()
        self.assertEqual(self.target.read_text(), '[{"name":"mine"}]')

    def test_a_file_that_existed_once_is_not_reseeded_from_a_stale_copy(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('[{"name":"mine"}]')
        self.run_step1()
        self.target.unlink()
        (self.ws / "crons").mkdir()
        (self.ws / "crons" / "h1.json").write_text('[{"name":"stale"}]')
        self.legacy.write_text(json.dumps(self.example))
        self.run_step1()
        self.assertNotIn("stale", self.names())
        self.assertFalse(self.names() & OPT_IN, self.names())


if __name__ == "__main__":
    unittest.main()
