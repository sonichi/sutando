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
import importlib.util
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

    def test_an_unusable_backup_name_is_skipped_not_reported(self):
        kept_dir = self.ws / "hosts" / "h1"
        (kept_dir / "crons.legacy.json").mkdir(parents=True)
        self.target.write_text('[{"name":"mine"}]')
        self.run_step1()
        self.target.unlink()
        out = self._seed_with_an_owner_legacy_job()
        self.assertIn(f"restored {self.target}", out)
        self.assertIn(f"not-enabled my-report kept at {kept_dir / 'crons.legacy.1.json'}", out)
        self.assertIn("my-report", (kept_dir / "crons.legacy.1.json").read_text())
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

    def test_a_new_notice_is_added_beside_an_unacknowledged_one(self):
        self.assertIn("not-enabled my-report", self._seed_with_an_owner_legacy_job())
        self.legacy.unlink()
        self.target.unlink()
        out = self.run_step1()
        self.assertIn(f"restored {self.target}", out)
        self.assertIn("not-enabled my-report", out)

    def test_an_unreadable_notice_record_is_set_aside_and_reported(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('[{"name":"mine"}]')
        self._notice_record().write_text('{"notices": [trunc')
        out = self.run_step1()
        bad = self.ws / "hosts" / "h1" / "crons-notice.bad.json"
        self.assertIn(f"notice-record-unreadable kept at {bad}", out)
        self.assertEqual(bad.read_text(), '{"notices": [trunc')
        lines = [n["line"] for n in json.loads(self._notice_record().read_text())["notices"]]
        self.assertEqual(lines, [f"notice-record-unreadable kept at {bad}"])

    def test_backup_names_are_bounded(self):
        kept_dir = self.ws / "hosts" / "h1"
        kept_dir.mkdir(parents=True)
        (kept_dir / "crons.legacy.json").write_text("[]")
        for n in range(1, 40):
            (kept_dir / f"crons.legacy.{n}.json").write_text("[]")
        out = self._seed_with_an_owner_legacy_job()
        latest = kept_dir / f"crons.legacy.{hashlib.sha256(self.legacy.read_bytes()).hexdigest()[:16]}.json"
        self.assertIn(f"not-enabled my-report kept at {latest}", out)
        self.assertIn("my-report", latest.read_text())
        self.assertFalse((kept_dir / "crons.legacy.40.json").exists())

    def _seeder(self):
        spec = importlib.util.spec_from_file_location(
            f"seed_crons_{id(self)}", self.repo / "skills" / "schedule-crons" / "scripts" / "seed_crons.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _seed_in_process(self, mod):
        return mod.seed(self.ws, "h1", self.repo / "skills" / "schedule-crons")

    def _owner_job(self) -> dict:
        own = {"name": "my-report", "cron": "0 9 * * *", "prompt": "x"}
        self.legacy.write_text(json.dumps([own]))
        return own

    def test_the_backup_is_saved_before_the_schedule_is_published(self):
        own = self._owner_job()
        mod = self._seeder()
        real = mod._publish

        def crash_after_target(path, content):
            published = real(path, content)
            if path == self.target:
                raise KeyboardInterrupt("crash after the schedule is published")
            return published
        mod._publish = crash_after_target
        with self.assertRaises(KeyboardInterrupt):
            self._seed_in_process(mod)
        backups = list((self.ws / "hosts" / "h1").glob("crons.legacy*.json"))
        self.assertTrue(backups and any(own in json.loads(b.read_text()) for b in backups), backups)
        self.assertIn("not-enabled my-report kept at", self.run_step1())

    def test_an_unsaved_backup_is_retried_while_its_notice_is_pending(self):
        own = self._owner_job()
        mod = self._seeder()
        real = mod.keep_legacy

        def fails(*_a):
            raise OSError("disk full")
        mod.keep_legacy = fails
        self._seed_in_process(mod)
        mod.keep_legacy = real
        out = self.run_step1()
        kept = self.ws / "hosts" / "h1" / "crons.legacy.json"
        self.assertIn(own, json.loads(kept.read_text()))
        rec = json.loads(self._notice_record().read_text())
        self.assertEqual([n["line"] for n in rec["notices"]], [f"not-enabled my-report kept at {kept}"])
        self.assertIn("not-enabled my-report kept at", out)

    def test_an_unsaved_backup_says_where_the_only_copy_is(self):
        self._owner_job()
        mod = self._seeder()

        def fails(*_a):
            raise OSError("disk full")
        mod.keep_legacy = fails
        self._seed_in_process(mod)
        line = json.loads(self._notice_record().read_text())["notices"][0]["line"]
        self.assertIn("only at", line)
        self.assertIn("an app update replaces it", line)

    def test_a_losing_seeder_never_removes_the_winners_notices(self):
        self._owner_job()
        winner, loser = self._seeder(), self._seeder()
        real = loser._publish

        def lose(path, content):
            if path == self.target:
                self._seed_in_process(winner)
                return real(path, content)
            return real(path, content)
        loser._publish = lose
        self.assertEqual(self._seed_in_process(loser)[0], "exists")
        lines = [n["line"] for n in json.loads(self._notice_record().read_text())["notices"]]
        self.assertTrue(any(x.startswith("not-enabled my-report") for x in lines), lines)
        self.assertEqual(len(self._queued_dms()), 0)
        self.run_step1()
        self.assertEqual(len(self._queued_dms()), 1)

    def test_a_seed_that_loses_to_the_owner_withdraws_its_notices(self):
        self._owner_job()
        mod = self._seeder()
        real = mod._publish

        def owner_first(path, content):
            if path == self.target:
                self.target.write_text('[{"name":"mine"}]')
            return real(path, content)
        mod._publish = owner_first
        self.assertEqual(self._seed_in_process(mod)[0], "exists")
        self.assertFalse(self._notice_record().exists())

    def test_seeder_runs_on_one_host_are_serialized(self):
        import fcntl
        host = self.ws / "hosts" / "h1"
        host.mkdir(parents=True)
        with open(host / ".crons-seed.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaises(subprocess.TimeoutExpired):
                subprocess.run(["bash", "-c", step1_command()], cwd=self.repo, capture_output=True,
                               text=True, timeout=2)
        self.assertFalse(self.target.exists())
        self.run_step1()
        self.assertTrue(self.target.exists())

    def test_a_recurring_restore_after_acknowledgement_is_queued_again(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('[{"name":"mine"}]')
        self.run_step1()
        self.target.unlink()
        self.assertIn("restored", self.run_step1())
        first = self._queued_dms()
        ack = subprocess.run(["python3", "skills/schedule-crons/scripts/seed_crons.py", "--notice-sent"],
                             cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(ack.returncode, 0, ack.stderr)
        self.target.unlink()
        self.assertIn("restored", self.run_step1())
        second = self._queued_dms()
        self.assertEqual(len(first), 1, first)
        self.assertEqual(len(second), 2, "a new episode is queued under a new outbox name")
        self.run_step1()
        self.assertEqual(self._queued_dms(), second, "retries of one episode reuse its name")

    def test_a_retried_backup_never_claims_jobs_the_legacy_file_no_longer_holds(self):
        self._owner_job()
        mod = self._seeder()

        def fails(*_a):
            raise OSError("disk full")
        mod.keep_legacy = fails
        self._seed_in_process(mod)
        self.legacy.write_text(json.dumps([{"name": "other", "cron": "0 1 * * *", "prompt": "y"}]))
        self._age_notice(2 * 86400)
        out = self.run_step1()
        line = next(x for x in out.splitlines() if x.startswith("not-enabled"))
        self.assertNotIn("kept at", line)
        self.assertIn("lost", line)
        self.assertIn("my-report", line)
        self.assertFalse((self.ws / "hosts" / "h1" / "crons.legacy.json").exists())
        bodies = " ".join(p.read_text() for p in self._queued_dms())
        self.assertIn("my-report", bodies)
        self.assertIn("app update", bodies)

    def test_a_retried_backup_reports_a_deleted_legacy_file_as_lost(self):
        self._owner_job()
        mod = self._seeder()

        def fails(*_a):
            raise OSError("disk full")
        mod.keep_legacy = fails
        self._seed_in_process(mod)
        self.legacy.unlink()
        self._age_notice(2 * 86400)
        out = self.run_step1()
        line = next(x for x in out.splitlines() if x.startswith("not-enabled"))
        self.assertIn("lost", line)
        self.assertIn("my-report", line)
        self.assertNotIn("only at", line)
        bodies = " ".join(p.read_text() for p in self._queued_dms())
        self.assertIn("app update", bodies)

    def test_overflow_backups_never_replace_one_a_notice_still_names(self):
        host = self.ws / "hosts" / "h1"
        host.mkdir(parents=True)
        (host / "crons.legacy.json").write_text("[]")
        for n in range(1, 21):
            (host / f"crons.legacy.{n}.json").write_text("[]")
        first = {"name": "first-job", "cron": "0 9 * * *", "prompt": "a"}
        self.legacy.write_text(json.dumps([first]))
        self.run_step1()
        self.target.unlink()
        second = {"name": "second-job", "cron": "0 9 * * *", "prompt": "b"}
        self.legacy.write_text(json.dumps([second]))
        self.run_step1()
        rec = json.loads(self._notice_record().read_text())
        kept = {n["names"][0]: n["kept"] for n in rec["notices"] if n.get("names")}
        self.assertNotEqual(kept["first-job"], kept["second-job"])
        self.assertIn(first, json.loads(Path(kept["first-job"]).read_text()))
        self.assertIn(second, json.loads(Path(kept["second-job"]).read_text()))

    def test_a_losing_run_never_drops_a_notice_an_earlier_run_recorded(self):
        self.assertIn("not-enabled my-report", self._seed_with_an_owner_legacy_job())
        self.target.unlink()
        mod = self._seeder()
        real = mod._publish

        def owner_first(path, content):
            if path == self.target:
                self.target.write_text('[{"name":"mine"}]')
            return real(path, content)
        mod._publish = owner_first
        self.assertEqual(self._seed_in_process(mod)[0], "exists")
        lines = [n["line"] for n in json.loads(self._notice_record().read_text())["notices"]]
        self.assertTrue(any(x.startswith("not-enabled my-report") for x in lines), lines)

    def test_a_migrated_notice_is_queued_once_even_if_saving_fails_after_the_enqueue(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('[{"name":"mine"}]')
        kept = self.ws / "hosts" / "h1" / "crons.legacy.json"
        self._notice_record().write_text(json.dumps({"lines": [f"not-enabled my-report kept at {kept}"]}))
        mod = self._seeder()
        real = mod._write_record

        def save_fails_after_enqueue(host_dir, record):
            if self._queued_dms():
                raise OSError("no space left on device")
            return real(host_dir, record)
        mod._write_record = save_fails_after_enqueue
        with self.assertRaises(OSError):
            mod.due_notices(self.ws, self.ws / "hosts" / "h1", 10**10)
        self.run_step1()
        self.assertEqual(len(self._queued_dms()), 1, self._queued_dms())

    def test_notice_sent_keeps_notices_never_shown(self):
        self._owner_job()
        self._seed_in_process(self._seeder())
        ack = subprocess.run(["python3", "skills/schedule-crons/scripts/seed_crons.py", "--notice-sent"],
                             cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(ack.returncode, 0, ack.stderr)
        self.assertIn("not-enabled my-report", self.run_step1())

    def test_a_failed_owner_dm_enqueue_is_reported(self):
        (self.ws / "results").write_text("not a directory")
        self._owner_job()
        out = subprocess.run(["bash", "-c", step1_command()], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("could not queue the owner DM", out.stderr)

    def test_the_same_notice_is_merged_not_repeated(self):
        self.assertIn("not-enabled my-report", self._seed_with_an_owner_legacy_job())
        (self.ws / "hosts" / "h1" / "crons.legacy.json").write_text("[]")
        self.target.unlink()
        self.run_step1()
        lines = [n["line"] for n in json.loads(self._notice_record().read_text())["notices"]]
        self.assertEqual(sum(x.startswith("not-enabled my-report") for x in lines), 1, lines)

    def test_unreadable_records_past_the_cap_do_not_block_the_seed(self):
        host = self.ws / "hosts" / "h1"
        host.mkdir(parents=True)
        (host / "crons-notice.bad.json").write_text("x")
        for n in range(1, 21):
            (host / f"crons-notice.bad.{n}.json").write_text("x")
        self._notice_record().write_text("{broken")
        out = subprocess.run(["bash", "-c", step1_command()], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(self.target.exists())
        self.assertIn(f"notice-record-unreadable kept at {host / 'crons-notice.bad.latest.json'}", out.stdout)

    def test_a_record_of_the_wrong_shape_is_reported(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('[{"name":"mine"}]')
        self._notice_record().write_text('{"notices": {"oops": 1}}')
        self.assertIn("notice-record-unreadable kept at", self.run_step1())

    def test_a_record_in_the_earlier_format_is_migrated(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('[{"name":"mine"}]')
        kept = self.ws / "hosts" / "h1" / "crons.legacy.json"
        self._notice_record().write_text(json.dumps({"lines": [f"not-enabled my-report kept at {kept}"]}))
        self.assertIn(f"not-enabled my-report kept at {kept}", self.run_step1())
        self.assertEqual(len(self._queued_dms()), 1)

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
