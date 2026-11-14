#!/usr/bin/env python3
"""A missing per-host crons.json must never be self-healed into jobs the owner did not opt into.

Runs `/schedule-crons` step 1 as the agent would: the step's last inline code span, executed by
bash in a scratch checkout whose `scripts/sutando-config.sh` points at a scratch workspace. The
installer-managed in-checkout `skills/schedule-crons/crons.json` holds the shipped sample jobs.
`crons.example.pre-opt-in.json` is the template as shipped before `opt_in` existed.

Run: python3 tests/schedule-crons-seed-policy.test.py
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "schedule-crons"
OPT_IN = {"morning-briefing", "example-digest"}
FIXTURES = REPO / "tests" / "fixtures" / "schedule-crons"
PRE_OPT_IN = FIXTURES / "crons.example.pre-opt-in.json"
OLDEST = FIXTURES / "crons.example.2b45993b.json"


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
        (self.repo / "src").symlink_to(REPO / "src")
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

    def test_oldest_shipped_template_is_not_called_owner_jobs(self):
        self.legacy.write_text(OLDEST.read_text())
        out = self.run_step1()
        self.assertNotIn("not-enabled", out)
        self.assertFalse(self.names() & OPT_IN, self.names())

    def test_history_holds_every_current_template_entry(self):
        history = {h["sha256"] for h in json.loads((SKILL / "crons.example.history.json").read_text())}
        for e in self.example:
            job = {k: v for k, v in e.items() if k != "opt_in"}
            blob = json.dumps(job, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            self.assertIn(hashlib.sha256(blob.encode()).hexdigest(), history,
                          f"append {e['name']}'s digest to crons.example.history.json")

    def test_a_second_keep_never_names_a_file_holding_other_content(self):
        kept_dir = self.ws / "hosts" / "h1"
        kept_dir.mkdir(parents=True)
        (kept_dir / "crons.legacy.json").write_text('[{"name":"older"}]')
        own = {"name": "my-report", "cron": "0 9 * * *", "prompt": "x"}
        self.legacy.write_text(json.dumps([own]))
        out = self.run_step1()
        path = Path(out.split("kept at ", 1)[1].strip())
        self.assertIn(own, json.loads(path.read_text()))
        self.assertEqual((kept_dir / "crons.legacy.json").read_text(), '[{"name":"older"}]')

    def test_a_restore_reuses_the_backup_already_holding_the_legacy_file(self):
        self.assertIn("not-enabled my-report", self._seed_with_an_owner_legacy_job())
        self.target.unlink()
        out = self.run_step1()
        kept_dir = self.ws / "hosts" / "h1"
        self.assertIn(f"kept at {kept_dir / 'crons.legacy.json'}", out)
        self.assertEqual(sorted(p.name for p in kept_dir.glob("crons.legacy*")), ["crons.legacy.json"])

    def test_a_failed_backup_still_tells_the_owner(self):
        kept_dir = self.ws / "hosts" / "h1"
        (kept_dir / "crons.legacy.json").mkdir(parents=True)
        self.target.write_text('[{"name":"mine"}]')
        self.run_step1()
        self.target.unlink()
        out = self._seed_with_an_owner_legacy_job()
        self.assertIn(f"restored {self.target}", out)
        self.assertIn(f"not-enabled my-report kept at {self.legacy.resolve()}", out)
        self.run_step1()
        self._age_notice(2 * 86400)
        self.assertIn("not-enabled my-report", self.run_step1())

    def test_a_restored_file_tells_the_owner_their_jobs_need_adding_back(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('[{"name":"mine"}]')
        self.run_step1()
        self.target.unlink()
        out = self.run_step1()
        self.assertIn(f"restored {self.target}", out)
        self.assertIn("main-loop", self.names())

    def test_a_first_seed_is_not_reported_as_restored(self):
        self.assertNotIn("restored", self.run_step1())

    def _seed_with_an_owner_legacy_job(self) -> str:
        self.legacy.write_text(json.dumps([{"name": "my-report", "cron": "0 9 * * *", "prompt": "x"}]))
        return self.run_step1()

    def _notice_record(self) -> Path:
        return self.ws / "hosts" / "h1" / "crons-notice.json"

    def _age_notice(self, seconds: int):
        rec = json.loads(self._notice_record().read_text())
        rec["last_shown"] -= seconds
        self._notice_record().write_text(json.dumps(rec))

    def _queued_dms(self) -> list:
        return sorted((self.ws / "results").glob("proactive-crons-notice-*.txt"))

    def test_an_unacknowledged_notice_is_kept_until_acknowledged(self):
        self.assertIn("not-enabled my-report", self._seed_with_an_owner_legacy_job())
        for _ in range(3):
            self.run_step1()
        self.assertTrue(self._notice_record().exists())
        self._age_notice(2 * 86400)
        self.assertIn("not-enabled my-report", self.run_step1())
        self.assertTrue(self._notice_record().exists(), "a replay is not an acknowledgement")
        ack = subprocess.run(["python3", "skills/schedule-crons/scripts/seed_crons.py", "--notice-sent"],
                             cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(ack.returncode, 0, ack.stderr)
        self.assertFalse(self._notice_record().exists())

    def test_a_pending_notice_is_replayed_at_most_once_a_day(self):
        self.assertIn("not-enabled my-report", self._seed_with_an_owner_legacy_job())
        self.assertNotIn("not-enabled", self.run_step1())
        self._age_notice(86400 + 60)
        self.assertIn("not-enabled my-report", self.run_step1())

    def test_each_notice_is_queued_once_to_the_owner_dm(self):
        self._seed_with_an_owner_legacy_job()
        dms = self._queued_dms()
        self.assertEqual(len(dms), 1, "queued on the seeding run")
        body = dms[0].read_text()
        self.assertTrue(body.startswith("[dm-only]\n"), body)
        self.assertIn("my-report", body)
        dms[0].unlink()  # the bridge delivered and archived it
        self.run_step1()
        self._age_notice(2 * 86400)
        self.run_step1()
        self.assertEqual(self._queued_dms(), [], "a delivered notice is not queued again")

    def test_a_failed_notice_write_commits_nothing_and_a_retry_regenerates_it(self):
        self._notice_record().mkdir(parents=True)
        self.legacy.write_text(json.dumps([{"name": "my-report", "cron": "0 9 * * *", "prompt": "x"}]))
        out = subprocess.run(["bash", "-c", step1_command()], cwd=self.repo, capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(self.target.exists(), "the config is committed only after its notice")
        self._notice_record().rmdir()
        self.assertIn("not-enabled my-report", self.run_step1())

    def test_an_acknowledged_notice_is_not_repeated(self):
        self.assertIn("not-enabled my-report", self._seed_with_an_owner_legacy_job())
        ack = subprocess.run(["python3", "skills/schedule-crons/scripts/seed_crons.py", "--notice-sent"],
                             cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(ack.returncode, 0, ack.stderr)
        self.assertNotIn("not-enabled", self.run_step1())

    def test_an_occupied_backup_name_is_never_reported_as_the_new_capture(self):
        kept_dir = self.ws / "hosts" / "h1"
        kept_dir.mkdir(parents=True)
        (kept_dir / "crons.legacy.json").write_text('[{"name":"older"}]')
        (kept_dir / "crons.legacy.1.json").write_text('[{"name":"older-still"}]')
        now = int(time.time())
        for t in range(now - 5, now + 60):
            (kept_dir / f"crons.legacy.{t}.json").write_text('[{"name":"older-dated"}]')
        own = {"name": "my-report", "cron": "0 9 * * *", "prompt": "x"}
        self.legacy.write_text(json.dumps([own]))
        out = self.run_step1()
        path = Path(out.split("kept at ", 1)[1].split("\n", 1)[0].strip())
        self.assertIn(own, json.loads(path.read_text()))
        self.assertEqual(json.loads((kept_dir / "crons.legacy.1.json").read_text()), [{"name": "older-still"}])

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
