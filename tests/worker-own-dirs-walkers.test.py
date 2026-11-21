#!/usr/bin/env python3
"""Readers that walk the queue also walk each converted pool worker's own folders (`dirs: "own"`).

- `connections.own_worker_homes`: one record read, every own-dir worker's folders; [] when unreadable.
- archive-stale-results sweeps each own results/ as it sweeps the shared one.
- health_snapshot's result check, pool supervision's owed count and the agent API's active rows find an
  own-dir worker's task and result in its folders (rows tagged with the worker); shared behaviour stays.

Run: python3 tests/worker-own-dirs-walkers.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "packages" / "ag2-sparrow"),
                str(ROOT / "skills" / "worker-pool" / "scripts")]
import connections as C, health_snapshot  # noqa: E401,E402

import pool_supervise as sup  # noqa: E402
from ag2_sparrow import agent_identity  # noqa: E402

W = "7c700e11" + "0" * 24
WORKER_AGENT = "@reviewer:alpha-fixture.net"
WORKER_INST = C.worker_instance_for("alpha-fixture.net", W)
OWN_ID, CORE_ID = f"task-{WORKER_INST}~abc", "task-1700000000000"


def row(instance, *, primary=False, **extra):
    return {"server": "alpha-fixture.net", "instance": instance, "gateway_url": "https://chat.alpha-fixture.net",
            "token_ref": f"AG2_REMOTE_TOKEN_{instance.upper().replace('-', '_')}", "primary": primary,
            "added_by": "owner", "added_at": "2026-11-16T00:00:00Z", "verified": True,
            "server_source": "gateway", **extra}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name).resolve()
        (self.ws / ".notes-migrated").touch()
        (self.ws / ".build_log-migrated").touch()
        self.own = self.ws / "agents" / agent_identity.root_segment(WORKER_AGENT)
        for d in (self.own / "tasks", self.own / "results", self.ws / "tasks", self.ws / "results"):
            d.mkdir(parents=True)
        self.write(dirs="own")

    def write(self, **worker_extra):
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "connections": [
            row("alpha-fixture-net", primary=True),
            row(WORKER_INST, worker=W, agent_mxid=WORKER_AGENT, **worker_extra)]}))


class Homes(Base):
    def test_own_shared_and_unreadable(self):
        self.assertEqual(C.own_worker_homes(self.ws),
                         [{"worker": W, "tasks": self.own / "tasks", "results": self.own / "results"}])
        self.write()
        self.assertEqual(C.own_worker_homes(self.ws), [])
        C.record_path(self.ws).write_text("{torn")
        self.assertEqual(C.own_worker_homes(self.ws), [])


class Archive(Base):
    def archiver(self):
        env = {"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(self.ws), "RETENTION_HOURS": "24"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("DRY_RUN", None)
            mod = _load("archiver_own", ROOT / "src" / "archive-stale-results.py")
        mod.paths_held_open = lambda paths: set()
        return mod

    def stale(self, path: Path):
        path.write_text("answer\n")
        old = time.time() - 48 * 3600
        os.utime(path, (old, old))

    def test_sweeps_shared_and_own_results(self):
        self.stale(self.ws / "results" / f"{CORE_ID}.txt")
        self.stale(self.own / "results" / f"{OWN_ID}.txt")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.archiver().main(), 0)
        self.assertFalse((self.own / "results" / f"{OWN_ID}.txt").exists())
        self.assertEqual(len(list(self.own.glob(f"results/archive-*/{OWN_ID}.txt"))), 1)
        self.assertEqual(len(list(self.ws.glob(f"results/archive-*/{CORE_ID}.txt"))), 1)
        self.assertIn(f"worker {W}'s own results/", out.getvalue())

    def test_missing_shared_results_still_sweeps_own_and_shared_only_is_unchanged(self):
        (self.ws / "results").rmdir()
        self.stale(self.own / "results" / f"{OWN_ID}.txt")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.archiver().main(), 0)
        self.assertIn("results/ missing", out.getvalue())
        self.assertFalse((self.own / "results" / f"{OWN_ID}.txt").exists())
        self.write()
        (self.ws / "results").mkdir()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.archiver().main()
        self.assertNotIn("own results", out.getvalue())


class HealthSnapshot(Base):
    def test_result_in_its_own_folder(self):
        self.assertFalse(health_snapshot._has_result(self.ws, OWN_ID))
        (self.own / "results" / f"{OWN_ID}.txt").write_text("done\n")
        self.assertTrue(health_snapshot._has_result(self.ws, OWN_ID))
        (self.ws / "results" / f"{CORE_ID}.txt").write_text("done\n")
        self.assertTrue(health_snapshot._has_result(self.ws, CORE_ID))


class HealthSnapshotHistory(Base):
    def archived(self, results: Path):
        (results / "archive").mkdir(exist_ok=True)
        (results / "archive" / f"{OWN_ID}.txt").write_text("done\n")

    def test_an_answer_archived_before_migration(self):
        self.archived(self.ws / "results")
        self.assertTrue(health_snapshot._has_result(self.ws, OWN_ID))

    def test_an_answer_archived_in_its_own_folders_before_undo(self):
        self.archived(self.own / "results")
        self.write()
        self.assertTrue(health_snapshot._has_result(self.ws, OWN_ID))

    def test_a_stale_live_result_in_the_other_home_is_not_one(self):
        (self.ws / "results" / f"{OWN_ID}.txt").write_text("stale\n")
        self.assertFalse(health_snapshot._has_result(self.ws, OWN_ID))


class Owed(Base):
    def test_own_inbox_tasks_without_a_ready_result(self):
        for tid in (OWN_ID, f"task-{WORKER_INST}~def"):
            (self.own / "tasks" / f"{tid}.txt").write_text(f"id: {tid}\ntask: hi\n")
        (self.own / "tasks" / f"task-{WORKER_INST}~ghi.claimed-core-1.txt").write_text("id: x\ntask: hi\n")
        (self.own / "results" / f"task-{WORKER_INST}~def.txt").write_text("done\n")
        self.assertEqual(sup.owed_task_ids(self.ws, W), [OWN_ID, f"task-{WORKER_INST}~ghi"])
        self.assertTrue(sup.work_outstanding(self.ws, W))

    def test_a_shared_dir_worker_keeps_its_sentinels(self):
        self.write()
        with mock.patch.object(sup, "sentinel_ids", return_value=(["task-s"], set())) as ids:
            self.assertEqual(sup.owed_task_ids(self.ws, W), ["task-s"])
        ids.assert_called_once_with(self.ws, W)

    def test_no_inbox_owes_nothing(self):
        (self.own / "tasks").rmdir()
        self.assertEqual(sup.owed_task_ids(self.ws, W), [])


class UnansweredInInbox(Base):
    """The one owed rule for a plain inbox, shared by pool supervision and the Stop hook's own-inbox scan."""

    def test_ids_without_a_ready_result_claimed_names_once_and_wake_turns(self):
        from delivery import task_dispatch
        tasks, results = self.own / "tasks", self.own / "results"
        (tasks / "task-a.txt").write_text("id: task-a\ntask: x\n")
        (tasks / "task-a.claimed-core-1.txt").write_text("id: task-a\ntask: x\n")
        (tasks / "task-b.txt").write_text("id: task-b\ntask: x\n")
        (results / "task-b.txt").write_text("done\n")
        (results / "task-w.txt").write_text("earlier turn\n")
        old = time.time() - 600
        os.utime(results / "task-w.txt", (old, old))
        (tasks / "task-w.txt").write_text("id: task-w\nwake_episode: 2\ntask: x\n")
        self.assertEqual([t for t, _f in task_dispatch.unanswered_in_inbox(tasks, results)], ["task-a", "task-w"])
        self.assertEqual(task_dispatch.unanswered_in_inbox(self.ws / "missing", results), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(task_dispatch._main(["unanswered-in", str(tasks), str(results)]), 0)
        self.assertEqual(out.getvalue().split("\n")[0].split("\t")[0], "task-a")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(task_dispatch._main(["unanswered-in", str(tasks), str(results), "x"]), 2)

    def test_an_unlistable_inbox_owes_nothing_listed(self):
        from delivery import task_dispatch
        with mock.patch.object(Path, "glob", side_effect=OSError):
            self.assertEqual(task_dispatch.unanswered_in_inbox(self.own / "tasks", self.own / "results"), [])

    def test_strict_lists_the_same_tasks_and_raises_where_the_default_lists_nothing(self):
        from delivery import task_dispatch
        tasks, results = self.own / "tasks", self.own / "results"
        for t in ("task-a", "task-b"):
            (tasks / f"{t}.txt").write_text(f"id: {t}\ntask: x\n")
        (results / "task-b.txt").write_text("done\n")
        (tasks / "task-c.txt").mkdir()
        (tasks / "notes.txt").write_text("not a task\n")
        ids = lambda **kw: [t for t, _f in task_dispatch.unanswered_in_inbox(tasks, results, **kw)]
        self.assertEqual((ids(), ids(strict=True)), (["task-a"], ["task-a"]))
        with self.assertRaises(OSError):
            task_dispatch.unanswered_in_inbox(self.ws / "missing", results, strict=True)
        real_stat = os.stat

        def stat_fails_on_a(path, *a, **kw):
            if Path(path).name == "task-a.txt":
                raise PermissionError(path)
            return real_stat(path, *a, **kw)
        # The listing succeeds; the file's own stat then fails.
        with mock.patch.object(task_dispatch.os, "stat", stat_fails_on_a), self.assertRaises(PermissionError):
            task_dispatch.unanswered_in_inbox(tasks, results, strict=True)

    def test_strict_never_takes_an_unreadable_wake_turn_as_answered(self):
        from delivery import task_dispatch
        tasks, results = self.own / "tasks", self.own / "results"
        (results / "task-w.txt").write_text("earlier turn\n")
        old = time.time() - 600
        os.utime(results / "task-w.txt", (old, old))
        (tasks / "task-w.txt").write_text("id: task-w\nwake_episode: 2\ntask: x\n")
        self.assertEqual([t for t, _f in task_dispatch.unanswered_in_inbox(tasks, results, strict=True)], ["task-w"])
        (tasks / "task-w.txt").chmod(0)
        self.addCleanup((tasks / "task-w.txt").chmod, 0o600)
        self.assertEqual(task_dispatch.unanswered_in_inbox(tasks, results), [])
        with self.assertRaises(PermissionError):
            task_dispatch.unanswered_in_inbox(tasks, results, strict=True)


class CompletionArchivedInTheFormerSharedFolder(Base):
    """After a switch, a task in the own inbox answered and archived under the shared results/ is not owed."""

    def setUp(self):
        super().setUp()
        (self.own / "tasks" / f"{OWN_ID}.txt").write_text(f"id: {OWN_ID}\ntask: hi\n")
        (self.ws / "results" / "archive").mkdir()
        self.archived = self.ws / "results" / "archive" / f"{OWN_ID}.txt"
        self.archived.write_text("answered before the switch\n")

    def test_the_inbox_rule_and_supervision_see_it_with_a_workspace(self):
        from delivery import task_dispatch
        tasks, results = self.own / "tasks", self.own / "results"
        self.assertEqual(task_dispatch.unanswered_in_inbox(tasks, results, workspace=self.ws), [])
        self.assertEqual([t for t, _f in task_dispatch.unanswered_in_inbox(tasks, results)], [OWN_ID])
        self.assertEqual(sup.owed_task_ids(self.ws, W), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(task_dispatch._main(["unanswered-in", str(tasks), str(results), "--workspace", str(self.ws)]), 0)
        self.assertEqual(out.getvalue(), "")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(task_dispatch._main(["unanswered-in", str(tasks), str(results), "--nope", "x"]), 2)

    def test_a_wake_turn_newer_than_that_archive_is_still_owed(self):
        from delivery import task_dispatch
        old = time.time() - 600
        os.utime(self.archived, (old, old))
        (self.own / "tasks" / f"{OWN_ID}.txt").write_text(f"id: {OWN_ID}\nwake_episode: 2\ntask: hi\n")
        self.assertEqual([t for t, _f in task_dispatch.unanswered_in_inbox(
            self.own / "tasks", self.own / "results", workspace=self.ws)], [OWN_ID])

    def test_the_active_row_reads_it_as_done(self):
        api = _load("agent_api_former", ROOT / "src" / "agent-api.py")
        with mock.patch.object(api, "TASK_DIR", self.ws / "tasks"), \
                mock.patch.object(api, "RESULT_DIR", self.ws / "results"), \
                mock.patch.object(api, "WORKSPACE_DIR", self.ws), \
                mock.patch.dict(api.task_history, {}, clear=True):
            row = next(r for r in api._active_task_rows() if r["id"] == OWN_ID)
        self.assertEqual((row["status"], row["result"].strip(), row["worker"]),
                         ("done", "answered before the switch", W))


class LiveResultsCountOnlyInTheirTasksHome(Base):
    def test_a_stale_shared_live_result_does_not_mark_a_pending_own_row_done(self):
        (self.own / "tasks" / f"{OWN_ID}.txt").write_text(f"id: {OWN_ID}\ntask: own one\nsource: chat\n")
        (self.ws / "results" / f"{OWN_ID}.txt").write_text("stale shared copy\n")
        (self.ws / "results" / f"{CORE_ID}.txt").write_text("core answer\n")
        api = _load("agent_api_live", ROOT / "src" / "agent-api.py")
        with mock.patch.object(api, "TASK_DIR", self.ws / "tasks"), \
                mock.patch.object(api, "RESULT_DIR", self.ws / "results"), \
                mock.patch.object(api, "WORKSPACE_DIR", self.ws), \
                mock.patch.dict(api.task_history, {}, clear=True):
            rows = {r["id"]: r for r in api._active_task_rows()}
            (self.own / "results" / f"{OWN_ID}.txt").write_text("own answer\n")
            api.task_history.pop(OWN_ID)
            (self.own / "tasks" / f"{OWN_ID}.txt").unlink()
            later = {r["id"]: r for r in api._active_task_rows()}
        self.assertEqual(rows[OWN_ID]["status"], "working")
        self.assertEqual((rows[CORE_ID]["status"], rows[CORE_ID]["result"]), ("done", "core answer"))
        self.assertEqual((later[OWN_ID]["status"], later[OWN_ID]["result"]), ("done", "own answer"))


class AProbeShapedId(Base):
    PROBE = "task-0123456789abcdef01"

    def test_resolves_to_the_own_home_before_and_after_it_is_answered_and_archived(self):
        (self.own / "tasks" / f"{self.PROBE}.txt").write_text(f"id: {self.PROBE}\ntask: probe\n")
        self.assertEqual(sup.owed_task_ids(self.ws, W), [self.PROBE])
        self.assertFalse(health_snapshot._has_result(self.ws, self.PROBE))
        (self.own / "results" / f"{self.PROBE}.txt").write_text("done\n")
        self.assertEqual(sup.owed_task_ids(self.ws, W), [])
        self.assertTrue(health_snapshot._has_result(self.ws, self.PROBE))
        for sub in ("tasks", "results"):
            (self.own / sub / "archive").mkdir()
            (self.own / sub / f"{self.PROBE}.txt").rename(self.own / sub / "archive" / f"{self.PROBE}.txt")
        self.assertTrue(C.task_home(self.PROBE, self.ws)["own"])
        self.assertTrue(health_snapshot._has_result(self.ws, self.PROBE))


class ActiveRows(Base):
    def rows(self):
        api = _load("agent_api_own", ROOT / "src" / "agent-api.py")
        with mock.patch.object(api, "TASK_DIR", self.ws / "tasks"), \
                mock.patch.object(api, "RESULT_DIR", self.ws / "results"), \
                mock.patch.object(api, "WORKSPACE_DIR", self.ws), \
                mock.patch.dict(api.task_history, {}, clear=True):
            first = api._active_task_rows()
            (self.own / "results" / f"{OWN_ID}.txt").write_text("own answer\n")
            return first, api._active_task_rows()

    def test_own_rows_carry_the_worker_and_its_result(self):
        (self.own / "tasks" / f"{OWN_ID}.txt").write_text(f"id: {OWN_ID}\ntask: own one\nsource: chat\n")
        (self.ws / "tasks" / f"{CORE_ID}.txt").write_text(f"id: {CORE_ID}\ntask: core one\nsource: chat\n")
        first, second = self.rows()
        by_id = {r["id"]: r for r in first}
        self.assertEqual((by_id[OWN_ID]["worker"], by_id[OWN_ID]["status"]), (W, "working"))
        self.assertNotIn("worker", by_id[CORE_ID])
        own = next(r for r in second if r["id"] == OWN_ID)
        self.assertEqual((own["status"], own["result"].strip()), ("done", "own answer"))

    def test_a_working_own_row_is_reconciled_from_its_folder(self):
        api = _load("agent_api_own2", ROOT / "src" / "agent-api.py")
        history = {OWN_ID: {"status": "working", "text": "x", "time": time.time(), "result": "", "source": "",
                            "worker": W}}
        (self.own / "results" / f"{OWN_ID}.txt").write_text("late answer\n")
        with mock.patch.object(api, "TASK_DIR", self.ws / "tasks"), \
                mock.patch.object(api, "RESULT_DIR", self.ws / "results"), \
                mock.patch.object(api, "WORKSPACE_DIR", self.ws), \
                mock.patch.dict(api.task_history, history, clear=True):
            rows = api._active_task_rows()
        self.assertEqual(next(r for r in rows if r["id"] == OWN_ID)["result"], "late answer")


if __name__ == "__main__":
    unittest.main()
