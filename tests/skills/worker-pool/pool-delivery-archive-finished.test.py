#!/usr/bin/env python3
"""A finished task's delivery sentinel moves to deliveries/<worker>/archive/; an open task keeps it.

Real filesystem, the production writer and the production readers (holder_of,
worker_holds, owned_task_ids), so a reader that starts looking inside archive/,
or a writer that moves an open task's hold, fails here.
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills/worker-pool/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))
import importlib  # noqa: E402

pd = importlib.import_module("pool_delivery")
wd = importlib.import_module("worker_delivery")
td = importlib.import_module("delivery.task_dispatch")

W = "worker-1"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for d in ("tasks/archive", "results/archive", f"deliveries/{W}", "state"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.inbox = self.root / "deliveries" / W
        self.archive = self.inbox / "archive"

    def sentinel(self, task_id, suffix=".txt"):
        p = self.inbox / f"{task_id}{suffix}"
        p.write_bytes(b"")
        return p

    def live(self, task_id, name=None):
        p = self.root / "tasks" / (name or f"{task_id}.txt")
        p.write_text(f"id: {task_id}\nsource: test\ntask: x\n", encoding="utf-8")
        return p

    def archived(self, task_id):
        (self.root / "tasks/archive" / f"{task_id}.txt").write_text(f"id: {task_id}\ntask: x\n")

    def answered(self, task_id):
        (self.root / "results/archive" / f"{task_id}.txt").write_text("the reply\n")

    def live_names(self):
        return sorted(n for n in os.listdir(self.inbox) if pd.parse_sentinel(n))

    def archived_names(self):
        return sorted(os.listdir(self.archive)) if self.archive.is_dir() else []


class TestArchiveFinished(Base):
    def fixture(self):
        self.answered("task-done"); self.archived("task-done"); self.sentinel("task-done")
        self.archived("task-dropped"); self.sentinel("task-dropped")             # archived, no result
        self.answered("task-acc"); self.archived("task-acc"); self.sentinel("task-acc", ".accepted")
        self.live("task-open"); self.sentinel("task-open")                       # pending
        self.live("task-held", "task-held.claimed-core-1.txt"); self.sentinel("task-held")
        self.live("task-sent"); self.answered("task-sent"); self.sentinel("task-sent")  # bridge not done
        self.sentinel("task-gone")                                               # missing everywhere

    def test_finished_sentinels_move_and_open_ones_stay(self):
        self.fixture()
        got = pd.archive_finished(self.root, W)
        self.assertEqual(sorted(got["archived"]), ["task-acc", "task-done", "task-dropped"])
        self.assertEqual(self.archived_names(), ["task-acc.accepted", "task-done.txt", "task-dropped.txt"])
        self.assertEqual(self.live_names(), ["task-gone.txt", "task-held.txt", "task-open.txt", "task-sent.txt"])
        self.assertEqual(got["kept"], {"pending": 1, "claimed": 1, "answered-payload-live": 1, "missing": 1})

    def test_dry_run_moves_nothing_and_counts_the_same(self):
        self.fixture()
        before = self.live_names()
        got = pd.archive_finished(self.root, W, dry_run=True)
        self.assertEqual(sorted(got["archived"]), ["task-acc", "task-done", "task-dropped"])
        self.assertEqual(self.live_names(), before)
        self.assertFalse(self.archive.exists())

    def test_a_second_pass_moves_nothing(self):
        self.fixture()
        pd.archive_finished(self.root, W)
        self.assertEqual(pd.archive_finished(self.root, W)["archived"], [])
        self.assertEqual(len(self.archived_names()), 3)

    def test_an_archived_record_of_the_same_name_is_never_overwritten(self):
        self.answered("task-done"); self.archived("task-done")
        self.archive.mkdir()
        (self.archive / "task-done.txt").write_text("earlier record")
        self.sentinel("task-done")
        pd.archive_finished(self.root, W)
        self.assertEqual((self.archive / "task-done.txt").read_text(), "earlier record")
        self.assertEqual(self.archived_names(), ["task-done.txt", "task-done.txt.1"])

    def test_a_symlink_at_a_sentinel_name_is_left_alone(self):
        self.answered("task-done"); self.archived("task-done")
        (self.inbox / "task-done.txt").symlink_to(self.root / "tasks/archive/task-done.txt")
        self.assertEqual(pd.archive_finished(self.root, W)["archived"], [])
        self.assertTrue((self.inbox / "task-done.txt").is_symlink())

    def test_a_task_re_queued_before_the_lock_keeps_its_hold(self):
        self.answered("task-done"); self.archived("task-done"); self.sentinel("task-done")
        real = pd.arbitration

        @contextlib.contextmanager
        def requeue_then_lock(ws, recipient, **kw):
            self.live("task-done")
            with real(ws, recipient, **kw) as dfd:
                yield dfd

        pd.arbitration = requeue_then_lock
        self.addCleanup(setattr, pd, "arbitration", real)
        got = pd.archive_finished(self.root, W)
        self.assertEqual(got["archived"], [])
        self.assertEqual(self.live_names(), ["task-done.txt"])

    def test_a_flagged_answer_moves_though_its_payload_is_still_live(self):
        self.live("task-sent"); self.answered("task-sent"); self.sentinel("task-sent")
        flag = pd.done_flag(self.root, W, "task-sent")
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("done\n")
        self.assertEqual(pd.archive_finished(self.root, W)["archived"], ["task-sent"])

    def test_an_id_subset_moves_only_those_tasks(self):
        self.fixture()
        got = pd.archive_finished(self.root, W, task_ids=["task-done", "task-open"])
        self.assertEqual(got["archived"], ["task-done"])
        self.assertEqual(got["kept"], {"pending": 1})
        self.assertIn("task-gone.txt", self.live_names())
        got = pd.archive_finished(self.root, W, task_ids=["task-gone"])
        self.assertEqual(got["archived"], ["task-gone"], "named explicitly, a missing task's sentinel moves")

    def test_prune_spent_archives_finished_work_it_used_to_keep(self):
        self.answered("task-done"); self.archived("task-done"); self.sentinel("task-done")
        self.live("task-open"); self.sentinel("task-open")
        acts = pd.prune_spent(self.root, W)
        self.assertEqual(acts["archived"], ["task-done"])
        self.assertEqual(self.live_names(), ["task-open.txt"])


class TestPruneSpentFailOpen(Base):
    """A broken `archive/` never stops `prune_spent`, and never costs a sentinel: it stays."""

    def spent(self):
        self.archived("task-1")
        return self.sentinel("task-1")

    def assert_still_kept(self):
        acts = pd.prune_spent(self.root, W)
        self.assertEqual(acts["archived"], [])
        self.assertIn("archive_error", acts)
        self.assertEqual(acts["kept"], ["task-1"])
        self.assertEqual(self.live_names(), ["task-1.txt"])

    def test_a_regular_file_at_archive(self):
        self.spent()
        self.archive.write_text("not a dir")
        self.assert_still_kept()
        self.assertEqual(self.archive.read_text(), "not a dir")

    def test_a_symlink_to_a_dir_at_archive(self):
        self.spent()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.archive.symlink_to(elsewhere)
        self.assert_still_kept()
        self.assertEqual(os.listdir(elsewhere), [])


class TestPruneArchive(Base):
    DAY = 86400

    def aged(self, name, days):
        p = self.archive / name
        p.write_bytes(b"")
        t = 1_000_000_000 - days * self.DAY
        os.utime(p, (t, t), follow_symlinks=False)
        return p

    def prune(self):
        return pd.prune_archive(self.root, W, now=1_000_000_000)

    def test_old_files_go_and_new_ones_stay(self):
        self.archive.mkdir()
        self.aged("task-old.txt", 15)
        self.aged("task-new.txt", 13)
        self.assertEqual(self.prune(), ["task-old.txt"])
        self.assertEqual(self.archived_names(), ["task-new.txt"])

    def test_the_default_age_is_fourteen_days(self):
        self.assertEqual(pd.ARCHIVE_MAX_AGE_DAYS, 14)

    def test_an_absent_archive_is_nothing_to_do(self):
        self.assertEqual(self.prune(), [])

    def test_links_and_subdirectories_inside_are_left_alone(self):
        self.archive.mkdir()
        target = self.root / "keep-me.txt"
        target.write_bytes(b"")
        t = 1_000_000_000 - 30 * self.DAY
        os.utime(target, (t, t))
        (self.archive / "task-link.txt").symlink_to(target)
        os.utime(self.archive / "task-link.txt", (t, t), follow_symlinks=False)
        sub = self.archive / "nested"
        sub.mkdir()
        self.aged("nested/task-deep.txt", 30)
        os.utime(sub, (t, t))
        self.assertEqual(self.prune(), [])
        self.assertTrue(target.exists())
        self.assertTrue((sub / "task-deep.txt").exists())

    def test_a_symlinked_archive_is_never_followed(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        old = elsewhere / "task-old.txt"
        old.write_bytes(b"")
        t = 1_000_000_000 - 30 * self.DAY
        os.utime(old, (t, t))
        self.archive.symlink_to(elsewhere)
        with self.assertRaises(OSError):
            self.prune()
        self.assertTrue(old.exists())
        acts = pd.prune_spent(self.root, W)
        self.assertEqual(acts["pruned"], [])
        self.assertIn("prune_error", acts)
        self.assertTrue(old.exists())

    def test_prune_spent_prunes_in_the_same_pass(self):
        self.archive.mkdir()
        old = self.aged("task-old.txt", 0)
        t = time.time() - 15 * self.DAY
        os.utime(old, (t, t))
        self.answered("task-done"); self.archived("task-done"); self.sentinel("task-done")
        acts = pd.prune_spent(self.root, W)
        self.assertEqual(acts["pruned"], ["task-old.txt"])
        self.assertEqual(acts["archived"], ["task-done"])
        self.assertEqual(self.archived_names(), ["task-done.txt"])

class TestReadersAfterArchive(Base):
    """A held, still-open task is never read as unowned; an archived one is not held."""

    def setUp(self):
        super().setUp()
        self.answered("task-done"); self.archived("task-done"); self.sentinel("task-done")
        self.live("task-open"); self.sentinel("task-open")
        pd.archive_finished(self.root, W)

    def test_holder_of(self):
        self.assertEqual(wd.holder_of(self.root, "task-open"), W)
        self.assertIsNone(wd.holder_of(self.root, "task-done"))

    def test_worker_holds(self):
        self.assertTrue(td.worker_holds(self.root / "deliveries", "task-open.txt"))
        self.assertFalse(td.worker_holds(self.root / "deliveries", "task-done.txt"))

    def test_owned_task_ids_and_pool_listings(self):
        self.assertEqual(td.owned_task_ids(self.root / "deliveries", W), ["task-open"])
        self.assertEqual([p.name for p in pd.pending(self.root, W)], ["task-open.txt"])
        self.assertEqual(pd.accepted(self.root, W), [])

    def test_the_stop_hook_still_reports_the_open_task(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SUTANDO_")}
        env.update(SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=str(self.root), SUTANDO_INSTANCE_ID=W,
                   SUTANDO_STOP_HOOK_WATCHER_GATE="0")
        ws = subprocess.run(["bash", str(REPO / "scripts/sutando-config.sh"), "workspace"],
                            capture_output=True, text=True, env=env).stdout.strip()
        self.assertEqual(os.path.realpath(ws), os.path.realpath(self.root), "hook must read the temp workspace")
        r = subprocess.run(["bash", str(REPO / "src/check-pending-tasks.sh")], input="{}",
                           capture_output=True, text=True, env=env, timeout=60)
        out = r.stdout + r.stderr
        self.assertIn("task-open", out)
        self.assertNotIn("task-done", out)


class TestCli(Base):
    def test_dry_run_prints_counts(self):
        self.answered("task-done"); self.archived("task-done"); self.sentinel("task-done")
        self.live("task-open"); self.sentinel("task-open")
        r = subprocess.run([sys.executable, str(SCRIPTS / "pool_delivery.py"), "--workspace", str(self.root),
                            "archive-finished", W, "--dry-run"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"recipient": W, "dry_run": True, "sentinels": 2,
                                                "archive": 1, "keep": {"pending": 1}})
        self.assertEqual(self.live_names(), ["task-done.txt", "task-open.txt"])

    def _main(self, *argv):
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = pd.main(["--workspace", str(self.root), *argv])
        return rc, json.loads(buf.getvalue())

    def test_main_in_process_dry_run_and_run(self):
        self.answered("task-done"); self.archived("task-done"); self.sentinel("task-done")
        self.live("task-open"); self.sentinel("task-open")
        rc, out = self._main("archive-finished", W, "--dry-run")
        self.assertEqual((rc, out["archive"], out["keep"]), (0, 1, {"pending": 1}))
        self.assertEqual(self.archived_names(), [])
        rc, out = self._main("--recipient", W, "archive-finished")
        self.assertEqual((rc, out["recipient"], out["archived"], out["dry_run"]), (0, W, 1, False))
        self.assertEqual(self.archived_names(), ["task-done.txt"])

    def test_run_reports_what_it_moved(self):
        self.answered("task-done"); self.archived("task-done"); self.sentinel("task-done")
        r = subprocess.run([sys.executable, str(SCRIPTS / "pool_delivery.py"), "--workspace", str(self.root),
                            "--recipient", W, "archive-finished"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["archived"], 1)
        self.assertEqual(self.archived_names(), ["task-done.txt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
