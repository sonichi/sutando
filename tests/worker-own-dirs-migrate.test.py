#!/usr/bin/env python3
"""`worker_lane.py migrate-dirs`: switching a pool worker onto its own folders, and back.

- Forward: creates `<ws>/agents/<segment>/{tasks,results}` (+ archive), sets `dirs: "own"`, has the declared
  handler empty the worker's deliveries folder (`pool_route_handler --migrate-dirs`), then moves the worker's
  unclaimed live results (its own lane's task results, its stamped proactive files) into its own results.
- The handler step: a task its own lane queued moves into its tasks folder and its sentinel is archived; a
  core-origin task is withdrawn (sentinel, attribution) and touched so the core picks it up; an answered
  core-origin task and a name its folder already holds are kept. Never replaces a file.
- `--undo`: clears the field and returns live tasks (touched) and results to the shared folders.
- Every step is idempotent; a refusal says why and a re-run finishes.

Run: python3 tests/worker-own-dirs-migrate.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
_spec = importlib.util.spec_from_file_location("worker_lane_bridge_suite_m", ROOT / "tests" / "worker-lane-sparrow-bridge.test.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)
C, W, worker_lane = S.C, S.W, S.worker_lane
SCRIPTS = ROOT / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pool_attribution as pat, pool_delivery as pd, pool_route_handler as prh  # noqa: E401,E402

from proactive_routing import proactive_filename  # noqa: E402


class Base(S.Base):
    def setUp(self):
        super().setUp()
        self.lane = self.row["instance"]
        self.own = self.agents / S.SEG
        self.mine, self.core = f"task-{self.lane}~a1", "task-1700000000000"

    def deliver(self, task_id, suffix=".txt"):
        (self.ws / "tasks").mkdir(parents=True, exist_ok=True)
        (self.ws / "tasks" / f"{task_id}.txt").write_text(f"id: {task_id}\ntask: x\n")
        d = pd.deliveries_dir(self.ws, W)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{task_id}{suffix}").write_text("")
        pat.record(self.ws, task_id, W)

    def migrate(self, **kw):
        kw.setdefault("handler", "")
        return worker_lane.migrate_dirs(W, self.ws, **kw)


class Forward(Base):
    def test_no_agent_row_is_refused(self):
        self.edit_worker_row(agent_mxid="")
        out, why = self.migrate()
        self.assertIn("has no agent identity row", why)
        self.assertIsNone(C.worker_dirs(W, self.ws))

    def test_creates_the_folders_sets_the_field_and_moves_its_live_results(self):
        shared = self.ws / "results"
        shared.mkdir(parents=True)
        stamped = proactive_filename(1, "ag2space", W)
        for name in (f"{self.mine}.txt", f"{self.core}.txt", stamped, "proactive-2.txt"):
            (shared / name).write_text("r")
        out, why = self.migrate()
        self.assertEqual(why, "")
        self.assertEqual(C.worker_dirs(W, self.ws), (self.own / "tasks", self.own / "results"))
        for d in ("tasks/archive", "results/archive"):
            self.assertTrue((self.own / d).is_dir())
        self.assertEqual((out["results_moved"], out["proactive_moved"]), ([f"{self.mine}.txt"], [stamped]))
        self.assertEqual(sorted(p.name for p in shared.glob("*.txt")), sorted([f"{self.core}.txt", "proactive-2.txt"]))
        again, why = self.migrate()
        self.assertEqual((why, again["results_moved"], again["proactive_moved"]), ("", [], []))

    def test_a_task_its_lane_queued_before_the_switch_moves_into_its_folder(self):
        (self.ws / "tasks").mkdir(parents=True)
        for tid in (self.mine, self.core):
            (self.ws / "tasks" / f"{tid}.txt").write_text("t")
        out, why = self.migrate()
        self.assertEqual((why, out["tasks_moved"]), ("", [f"{self.mine}.txt"]))
        home = C.task_home(self.mine, self.ws)
        self.assertTrue((home["tasks"] / f"{self.mine}.txt").is_file())
        self.assertTrue((self.ws / "tasks" / f"{self.core}.txt").is_file())
        import stat
        self.assertEqual(stat.S_IMODE(self.own.stat().st_mode), 0o700)

    def test_a_task_still_held_by_a_delivery_or_an_unreadable_folder_stays(self):
        self.deliver(self.mine)
        from delivery import task_dispatch
        self.assertEqual(worker_lane._queued_for(W, self.ws), [])
        with mock.patch.object(task_dispatch, "owned_task_ids", side_effect=task_dispatch.WorkerHoldUnreadable("io")):
            self.assertEqual(worker_lane._queued_for(W, self.ws), [])

    def test_a_record_write_refusal_stops_it(self):
        with mock.patch.object(C, "set_worker_dirs", side_effect=C.ConnectionsError("record busy")):
            self.assertEqual(self.migrate()[1], "record busy")

    def test_the_handler_runs_with_the_worker_and_reports(self):
        seen = []

        def run(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, '{"moved": ["t"]}\n', "")
        out, why = self.migrate(handler="/h", run=run)
        self.assertEqual((why, out["deliveries"]), ("", {"moved": ["t"]}))
        self.assertEqual(seen, [["/h", "--workspace", str(self.ws), "--migrate-dirs", "--worker", W]])
        out, _ = self.migrate(handler="/h", run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "plain", ""))
        self.assertEqual(out["deliveries"], "plain")

    def test_a_handler_refusal_or_failure_says_rerun(self):
        _, why = self.migrate(handler="/h", run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "nope"))
        self.assertIn("refused (exit 1: nope); re-run", why)
        _, why = self.migrate(handler="/h", run=mock.Mock(side_effect=OSError("gone")))
        self.assertIn("did not run", why)
        self.assertIsNotNone(C.worker_dirs(W, self.ws), "the field stays set: a re-run finishes")

    def test_a_failed_handler_lookup_switches_nothing(self):
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("bash", 10)):
            out, why = worker_lane.migrate_dirs(W, self.ws)
        self.assertIn("lookup failed", why)
        self.assertIn("nothing was switched", why)
        self.assertIsNone(C.worker_dirs(W, self.ws))

    def test_a_declared_but_unusable_handler_switches_nothing(self):
        cfg = self.ws / "state" / "task-event-handler.json"
        cfg.write_text(json.dumps({"handler": str(self.ws / "missing")}))
        out, why = worker_lane.migrate_dirs(W, self.ws)
        self.assertIn("declared but unusable", why)
        self.assertIsNone(C.worker_dirs(W, self.ws))
        self.assertEqual(worker_lane.declared_handler(self.ws), "")

    def test_no_handler_with_owed_deliveries_switches_nothing(self):
        self.deliver(self.core)
        out, why = worker_lane.migrate_dirs(W, self.ws)
        self.assertIn("1 task(s) delivered", why)
        self.assertIsNone(C.worker_dirs(W, self.ws))
        from delivery import task_dispatch
        with mock.patch.object(task_dispatch, "owned_task_ids", side_effect=task_dispatch.WorkerHoldUnreadable("io")):
            self.assertIn("io; nothing was switched", worker_lane.migrate_dirs(W, self.ws)[1])

    def test_no_handler_and_no_deliveries_switches(self):
        out, why = worker_lane.migrate_dirs(W, self.ws)
        self.assertEqual(why, "")
        self.assertIsNotNone(C.worker_dirs(W, self.ws))

    def test_the_declared_handler_is_found(self):
        handler = self.declare_handler()
        self.assertEqual(worker_lane.handler_lookup(self.ws), (str(handler), ""))

    def test_with_the_real_handler_its_delivered_work_moves(self):
        self.deliver(self.mine)
        self.deliver(self.core)
        out, why = self.migrate(handler=str(SCRIPTS / "pool_route_handler.py"))
        self.assertEqual(why, "")
        self.assertEqual(out["deliveries"], {"kept": {}, "moved": [self.mine], "returned": [self.core]})
        self.assertTrue((self.own / "tasks" / f"{self.mine}.txt").is_file())


class TheHandlerStep(Base):
    def setUp(self):
        super().setUp()
        C.set_worker_dirs(W, True, workspace=self.ws)

    def handle(self, worker=W):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = prh.main(["--workspace", str(self.ws), "--migrate-dirs", "--worker", worker])
        return rc, json.loads(out.getvalue()) if rc == 0 else None

    def test_own_lane_work_moves_and_core_work_is_handed_back(self):
        self.deliver(self.mine, ".accepted")
        self.deliver(self.core)
        os.utime(self.ws / "tasks" / f"{self.core}.txt", (1, 1))
        rc, out = self.handle()
        self.assertEqual((rc, out), (0, {"kept": {}, "moved": [self.mine], "returned": [self.core]}))
        d = pd.deliveries_dir(self.ws, W)
        self.assertEqual(sorted(p.name for p in d.iterdir() if p.is_file() and not p.name.startswith(".")), [])
        self.assertTrue((d / "archive" / f"{self.mine}.accepted").is_file())
        self.assertTrue((self.own / "tasks" / f"{self.mine}.txt").is_file())
        self.assertFalse((self.ws / "tasks" / f"{self.mine}.txt").exists())
        self.assertEqual(pat.worker_for_task(self.ws, self.mine), W)
        self.assertIsNone(pat.worker_for_task(self.ws, self.core))
        self.assertGreater((self.ws / "tasks" / f"{self.core}.txt").stat().st_mtime, 1)
        self.assertEqual(self.handle(), (0, {"kept": {}, "moved": [], "returned": []}))

    def test_an_answered_core_task_and_a_taken_name_are_kept(self):
        self.deliver(self.mine)
        self.deliver(self.core)
        (self.ws / "results").mkdir(parents=True, exist_ok=True)
        (self.ws / "results" / f"{self.core}.txt").write_text("answer")
        (self.own / "tasks").mkdir(parents=True)
        (self.own / "tasks" / f"{self.mine}.txt").write_text("already here")
        rc, out = self.handle()
        self.assertEqual(out["kept"], {self.core: "answered", self.mine: "own-folder-holds-the-name"})
        self.assertEqual((self.own / "tasks" / f"{self.mine}.txt").read_text(), "already here")
        self.assertTrue((self.ws / "tasks" / f"{self.mine}.txt").is_file())

    def test_an_own_lane_task_already_answered_and_archived_only_archives_its_sentinel(self):
        self.deliver(self.mine)
        (self.ws / "tasks" / f"{self.mine}.txt").unlink()
        rc, out = self.handle()
        self.assertEqual(out["moved"], [self.mine])
        self.assertFalse((self.own / "tasks" / f"{self.mine}.txt").exists())

    def test_a_worker_without_own_folders_or_an_unreadable_folder_is_refused(self):
        C.set_worker_dirs(W, False, workspace=self.ws)
        self.assertEqual(self.handle()[0], 1)
        C.set_worker_dirs(W, True, workspace=self.ws)
        with mock.patch.object(pd, "migrate_to_own_dirs", side_effect=OSError("io")):
            self.assertEqual(self.handle()[0], 1)

    def test_a_routing_call_still_needs_a_task_file(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            prh.main(["--workspace", str(self.ws)])


class Undo(Base):
    def test_returns_live_tasks_and_results_and_clears_the_field(self):
        self.migrate()
        (self.own / "tasks" / f"{self.mine}.txt").write_text("t")
        os.utime(self.own / "tasks" / f"{self.mine}.txt", (1, 1))
        answered = f"task-{self.lane}~b2"
        (self.own / "tasks" / f"{answered}.txt").write_text("t")
        (self.own / "results" / "archive").mkdir(parents=True, exist_ok=True)
        (self.own / "results" / "archive" / f"{answered}.txt").write_text("done")
        (self.own / "results" / "task-x.txt").write_text("r")
        out, why = self.migrate(undo=True)
        self.assertEqual((why, out["tasks_returned"], out["results_returned"], out["respawn_needed"], out["lane"]),
                         ("", [f"{self.mine}.txt"], ["task-x.txt"], True, "not-running"))
        self.assertTrue((self.own / "tasks" / f"{answered}.txt").is_file(), "an answered task stays")
        self.assertIsNone(C.worker_dirs(W, self.ws))
        self.assertGreater((self.ws / "tasks" / f"{self.mine}.txt").stat().st_mtime, 1)
        self.assertTrue((self.ws / "results" / "task-x.txt").is_file())
        again, why = self.migrate(undo=True)
        self.assertEqual((why, again["tasks_returned"], again["results_returned"]), ("", [], []))

    def test_never_replaces_a_shared_file(self):
        self.migrate()
        (self.own / "results" / "task-x.txt").write_text("own")
        (self.ws / "results").mkdir(parents=True, exist_ok=True)
        (self.ws / "results" / "task-x.txt").write_text("shared")
        out, _ = self.migrate(undo=True)
        self.assertEqual(out["results_returned"], [])
        self.assertEqual((self.ws / "results" / "task-x.txt").read_text(), "shared")


class TheLane(Base):
    def test_a_running_wrapper_lane_is_stopped_so_the_keeper_restarts_it_switched(self):
        from _helpers import bridge_lease
        proc = bridge_lease.start(self.ws, self.lane)
        self.addCleanup(bridge_lease.stop, proc)
        out, why = self.migrate(procs={})
        self.assertEqual(why, "")
        self.assertEqual(out["lane"], f"stopped pid {proc.pid}; the lane keeper starts it again")
        self.assertIsNotNone(proc.wait(timeout=10))
        self.assertEqual(C.bridge_pid(self.lane, self.ws), 0)

    def test_a_declared_bridge_is_waited_for_until_sparrowd_replaces_it(self):
        pids = iter([4242, 4242, C.BridgeLeaseIndeterminate(self.lane, "x"), 4343])

        def holder(*a):
            v = next(pids)
            if isinstance(v, Exception):
                raise v
            return v
        slept = []
        with mock.patch.object(C, "bridge_pid", side_effect=holder), \
                mock.patch.object(worker_lane, "held_by", return_value="declared-bridge"):
            self.assertEqual(worker_lane.relaunch_lane(W, self.ws, procs={}, sleep=slept.append),
                             ("restarted-by-sparrowd", ""))
        self.assertEqual(len(slept), 2)

    def test_a_late_move_by_the_old_bridge_is_drained_after_it_is_gone(self):
        self.migrate()
        stray = f"task-{self.lane}~late.txt"
        real = worker_lane._stop_unless_declared

        def old_bridge_moves_then_exits(*a, **k):
            (self.own / "results" / stray).write_text("reply")  # the old bridge's last adopt
            return real(*a, **k)
        with mock.patch.object(worker_lane, "_stop_unless_declared", old_bridge_moves_then_exits):
            out, why = self.migrate(undo=True)
        self.assertEqual(why, "")
        self.assertIn(stray, out["results_returned"])
        self.assertTrue((self.ws / "results" / stray).is_file())
        self.assertFalse((self.own / "results" / stray).exists())

    def test_the_field_is_written_under_the_lock_the_keeper_starts_take(self):
        seen = []
        real = C._records_lock

        def watched(ws):
            seen.append("lock")
            return real(ws)
        with mock.patch.object(C, "_records_lock", watched), \
                mock.patch.object(C, "set_worker_dirs", wraps=C.set_worker_dirs) as setter:
            self.migrate()
        self.assertEqual(seen[0], "lock")
        self.assertTrue(setter.called)

    def test_a_lingering_declared_bridge_moves_nothing(self):
        (self.ws / "results").mkdir(parents=True)
        (self.ws / "results" / f"{self.mine}.txt").write_text("r")
        clock = iter(range(0, 1000, 100))
        with mock.patch.object(C, "bridge_pid", return_value=4242), \
                mock.patch.object(worker_lane, "held_by", return_value="declared-bridge"), \
                mock.patch.object(worker_lane.time, "sleep", lambda s: None), \
                mock.patch.object(worker_lane.time, "monotonic", lambda: next(clock)):
            out, why = self.migrate(procs={})
        self.assertIn("pending: sparrowd has not replaced the declared bridge (pid 4242)", why)
        self.assertNotIn("results_moved", out)
        self.assertTrue((self.ws / "results" / f"{self.mine}.txt").is_file())

    def test_a_lane_that_will_not_stop_or_cannot_be_told_says_rerun(self):
        with mock.patch.object(C, "bridge_pid", side_effect=C.BridgeLeaseIndeterminate(self.lane, "torn")):
            self.assertIn("cannot be told", self.migrate()[1])
        with mock.patch.object(C, "bridge_pid", return_value=4242), \
                mock.patch.object(C, "_stop_row", side_effect=C.BridgeStillRunning(self.lane, 4242)):
            out, why = self.migrate(undo=True, procs={})
        self.assertIn("did not stop", why)


class TheCli(Base):
    def test_prints_one_json_line(self):
        rc, said = self.cli("migrate-dirs", "--worker", W)
        line = json.loads(said)
        self.assertEqual((rc, line["ok"], line["undo"]), (0, True, False))
        rc, said = self.cli("migrate-dirs", "--worker", W, "--undo")
        self.assertEqual((rc, json.loads(said)["undo"]), (0, True))

    def test_a_refusal_exits_1_with_why(self):
        self.edit_worker_row(agent_mxid="")
        rc, said = self.cli("migrate-dirs", "--worker", W)
        self.assertEqual((rc, json.loads(said)["ok"]), (1, False))
        self.assertIn("why", json.loads(said))

    def test_the_live_txt_listing_survives_an_unreadable_folder(self):
        with mock.patch.object(Path, "glob", side_effect=OSError):
            self.assertEqual(worker_lane._live_txt(self.ws), [])


if __name__ == "__main__":
    unittest.main()
