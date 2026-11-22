#!/usr/bin/env python3
"""`worker_lane.py migrate-dirs`: switching a pool worker onto its own folders, and back.

- Forward: creates `<ws>/agents/<segment>/{tasks,results}` (+ archive), sets `dirs: "migrating"`, has the
  declared handler empty the worker's deliveries folder (`pool_route_handler --migrate-dirs`), and only then
  sets `dirs: "own"`, relaunches the lane, empties what the old lane left and moves the worker's unclaimed
  live results (its own lane's task results, its stamped proactive files) into its own results. A handler
  that fails, or work the worker accepted and still owes, leaves it "migrating" on the shared folders.
- The handler step: a task its own lane queued moves into its tasks folder and its sentinel is archived; a
  core-origin task is withdrawn (sentinel, attribution) and touched so the core picks it up; an answered
  core-origin task and a name its folder already holds are kept. While any task it accepted is unanswered,
  nothing moves (`owed`). Never replaces a file.
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
            return subprocess.CompletedProcess(argv, 0, '{"moved": ["t"], "owed": []}\n', "")
        out, why = self.migrate(handler="/h", run=run)
        self.assertEqual((why, out["deliveries"], out["state"]), ("", {"moved": ["t"], "owed": []}, "own"))
        # Once before the switch, once after the lane is relaunched for what the old lane left.
        self.assertEqual(seen, [["/h", "--workspace", str(self.ws), "--migrate-dirs", "--worker", W]] * 2)
        C.set_worker_dirs(W, False, workspace=self.ws)
        out, why = self.migrate(handler="/h", run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "plain", ""))
        self.assertEqual((out["deliveries"], out["state"]), ("plain", "migrating"))
        self.assertIn("did not say what", why)
        self.assertIsNone(C.worker_dirs(W, self.ws))

    def test_a_handler_refusal_or_failure_says_rerun(self):
        _, why = self.migrate(handler="/h", run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "nope"))
        self.assertIn("refused (exit 1: nope); it stays on the shared folders; re-run", why)
        _, why = self.migrate(handler="/h", run=mock.Mock(side_effect=OSError("gone")))
        self.assertIn("did not run", why)
        self.assertIsNone(C.worker_dirs(W, self.ws), "not switched: it stays on the shared folders")
        self.assertEqual(C.worker_dirs_state(W, self.ws), "migrating", "a re-run finishes")
        out, why = self.migrate(handler="/h", run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '{"owed": []}', ""))
        self.assertEqual((why, out["state"]), ("", "own"))

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
        self.assertEqual(out["deliveries"], {"kept": {}, "moved": [self.mine], "owed": [], "returned": [self.core]})
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
        self.deliver(self.mine)
        self.deliver(self.core)
        os.utime(self.ws / "tasks" / f"{self.core}.txt", (1, 1))
        rc, out = self.handle()
        self.assertEqual((rc, out), (0, {"kept": {}, "moved": [self.mine], "owed": [], "returned": [self.core]}))
        d = pd.deliveries_dir(self.ws, W)
        self.assertEqual(sorted(p.name for p in d.iterdir() if p.is_file() and not p.name.startswith(".")), [])
        self.assertTrue((d / "archive" / f"{self.mine}.txt").is_file())
        self.assertTrue((self.own / "tasks" / f"{self.mine}.txt").is_file())
        self.assertFalse((self.ws / "tasks" / f"{self.mine}.txt").exists())
        self.assertEqual(pat.worker_for_task(self.ws, self.mine), W)
        self.assertIsNone(pat.worker_for_task(self.ws, self.core))
        self.assertGreater((self.ws / "tasks" / f"{self.core}.txt").stat().st_mtime, 1)
        self.assertEqual(self.handle(), (0, {"kept": {}, "moved": [], "owed": [], "returned": []}))

    def test_while_it_owes_accepted_work_nothing_moves(self):
        self.deliver(self.mine, ".accepted")
        self.deliver(self.core)
        rc, out = self.handle()
        self.assertEqual((rc, out), (0, {"kept": {self.mine: "accepted"}, "moved": [], "owed": [self.mine],
                                         "returned": []}))
        d = pd.deliveries_dir(self.ws, W)
        self.assertTrue((d / f"{self.mine}.accepted").is_file() and (d / f"{self.core}.txt").is_file())
        self.assertTrue((self.ws / "tasks" / f"{self.mine}.txt").is_file())

    def test_a_core_task_the_worker_accepted_stays_its_own(self):
        self.deliver(self.core, ".accepted")
        rc, out = self.handle()
        self.assertEqual((out["kept"], out["owed"]), ({self.core: "accepted"}, [self.core]))
        self.assertTrue((pd.deliveries_dir(self.ws, W) / f"{self.core}.accepted").is_file())
        self.assertEqual(pat.worker_for_task(self.ws, self.core), W)

    def test_an_interrupted_hand_back_is_finished_by_a_re_run(self):
        self.deliver(self.core)
        with mock.patch.object(pd, "_withdraw_locked", side_effect=OSError("disk")):
            self.assertEqual(self.handle()[0], 1)
        self.assertIsNone(pat.worker_for_task(self.ws, self.core), "attribution went first")
        self.assertTrue((pd.deliveries_dir(self.ws, W) / f"{self.core}.txt").is_file(), "the sentinel stays")
        self.assertEqual(self.handle(), (0, {"kept": {}, "moved": [], "owed": [], "returned": [self.core]}))

    def test_an_answered_core_task_and_a_taken_name_are_kept(self):
        self.deliver(self.mine)
        self.deliver(self.core)
        (self.ws / "results").mkdir(parents=True, exist_ok=True)
        (self.ws / "results" / f"{self.core}.txt").write_text("answer")
        (self.own / "tasks").mkdir(parents=True)
        (self.own / "tasks" / f"{self.mine}.txt").write_text("already here")
        rc, out = self.handle()
        self.assertEqual(out["kept"], {self.core: "completed", self.mine: "own-folder-holds-the-name"})
        self.assertEqual((self.own / "tasks" / f"{self.mine}.txt").read_text(), "already here")
        self.assertTrue((self.ws / "tasks" / f"{self.mine}.txt").is_file())

    def test_without_a_no_replace_rename_a_delivered_own_task_fails_the_run(self):
        import errno
        import undelivered_quarantine
        self.deliver(self.mine)
        with mock.patch.object(undelivered_quarantine, "rename_noreplace",
                               side_effect=FileExistsError(errno.ENOTSUP, "no primitive")):
            self.assertEqual(self.handle()[0], 1)
        self.assertTrue((self.ws / "tasks" / f"{self.mine}.txt").is_file())
        self.assertTrue((pd.deliveries_dir(self.ws, W) / f"{self.mine}.txt").is_file(), "the sentinel stays")
        self.assertEqual(self.handle(), (0, {"kept": {}, "moved": [self.mine], "owed": [], "returned": []}))

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


class MoveFailures(Base):
    def test_a_failed_move_is_reported_and_asks_for_a_re_run(self):
        import undelivered_quarantine
        (self.ws / "tasks").mkdir(parents=True)
        (self.ws / "tasks" / f"{self.mine}.txt").write_text("t")
        with mock.patch.object(undelivered_quarantine, "rename_noreplace", side_effect=PermissionError("denied")):
            out, why = self.migrate()
        self.assertIn("1 file(s) could not be moved; re-run", why)
        self.assertIn(f"{self.mine}.txt: PermissionError", out["move_failures"][0])
        out, why = self.migrate()
        self.assertEqual((why, out["tasks_moved"]), ("", [f"{self.mine}.txt"]))

    def test_a_result_or_proactive_file_that_will_not_move_fails_the_run(self):
        import errno
        import undelivered_quarantine
        (self.ws / "results").mkdir(parents=True)
        stamped = proactive_filename(1, "ag2space", W)
        for name in (f"{self.mine}.txt", stamped):
            (self.ws / "results" / name).write_text("r")
        with mock.patch.object(undelivered_quarantine, "rename_noreplace", side_effect=PermissionError("denied")):
            out, why = self.migrate()
        self.assertIn("2 file(s) could not be moved; re-run", why)
        self.assertEqual(sorted(f.split(":")[0] for f in out["move_failures"]), sorted([f"{self.mine}.txt", stamped]))
        undelivered_quarantine._REPORTED.discard("worker-lane:adopt")
        self.addCleanup(undelivered_quarantine._REPORTED.discard, "worker-lane:adopt")
        with mock.patch.object(undelivered_quarantine, "rename_noreplace",
                               side_effect=FileExistsError(errno.ENOTSUP, "no primitive")):
            out, why = self.migrate()
        self.assertIn("could not be moved", why)
        self.assertTrue(all("no no-replace rename" in f for f in out["move_failures"]))
        out, why = self.migrate()
        self.assertEqual((why, out["results_moved"], out["proactive_moved"]), ("", [f"{self.mine}.txt"], [stamped]))

    def test_undo_reports_a_failed_move_too(self):
        import errno
        import undelivered_quarantine
        self.migrate()
        (self.own / "results" / "task-x.txt").write_text("r")
        nothing = FileExistsError(errno.ENOTSUP, "no primitive")
        with mock.patch.object(undelivered_quarantine, "rename_noreplace", side_effect=nothing):
            out, why = self.migrate(undo=True)
        self.assertIn("could not be moved", why)
        self.assertIn("no no-replace rename", out["move_failures"][0])


class TheLane(Base):
    def started(self, record):
        calls = []

        def start(instance, ws):
            calls.append((instance, Path(ws), C.worker_dirs(W, self.ws), C.bridge_pid(self.lane, self.ws)))
            return record
        return calls, start

    def test_a_running_wrapper_lane_is_stopped_and_started_again_switched(self):
        from _helpers import bridge_lease
        proc = bridge_lease.start(self.ws, self.lane)
        self.addCleanup(bridge_lease.stop, proc)
        calls, start = self.started({"ok": True, "lease": "held", "pid": 5151})
        out, why = self.migrate(procs={}, start=start)
        self.assertEqual(why, "")
        self.assertEqual(out["lane"], f"stopped pid {proc.pid}; started again (pid 5151)")
        self.assertIsNotNone(proc.wait(timeout=10))
        self.assertEqual(calls, [(self.lane, self.ws, (self.own / "tasks", self.own / "results"), 0)],
                         "started once, after the old bridge is gone and with the field written")

    def test_the_restart_runs_in_the_same_hold_of_the_record_lock(self):
        from _helpers import bridge_lease
        proc = bridge_lease.start(self.ws, self.lane)
        self.addCleanup(bridge_lease.stop, proc)
        held = []

        def start(instance, ws):
            key = os.path.abspath(str(C._lock_path(Path(ws))))
            held.append((getattr(C._LOCK_DEPTH, "held", None) or {}).get(key, 0))
            return {"ok": True, "lease": "held", "pid": 1}
        self.migrate(procs={}, start=start)
        self.assertEqual(len(held), 1)
        self.assertGreater(held[0], 0, "no keeper can start the lane between the stop and this start")

    def test_a_lane_a_keeper_already_restarted_is_not_started_twice(self):
        from _helpers import bridge_lease
        proc = bridge_lease.start(self.ws, self.lane)
        self.addCleanup(bridge_lease.stop, proc)
        calls, start = self.started({"ok": True, "lease": "already_running", "pid": 6262})
        out, why = self.migrate(procs={}, start=start)
        self.assertEqual((why, out["lane"]), ("", f"stopped pid {proc.pid}; already running again (pid 6262)"))
        self.assertEqual(len(calls), 1)

    def test_the_default_start_is_connections_start_which_spawns_nothing_for_a_held_lease(self):
        from _helpers import bridge_lease
        proc = bridge_lease.start(self.ws, self.lane)
        self.addCleanup(bridge_lease.stop, proc)
        spawned = []
        rec = C.start(self.lane, repo=ROOT, logs_dir=self.ws / "logs", workspace=self.ws,
                      spawn=lambda *a: spawned.append(a))
        self.assertEqual((rec["lease"], rec["pid"], spawned), ("already_running", proc.pid, []))
        with mock.patch.object(C, "start", return_value={"ok": True, "lease": "held", "pid": 7}) as st:
            self.assertEqual(worker_lane._start_lane(self.lane, self.ws)["pid"], 7)
        self.assertEqual((st.call_args.args, st.call_args.kwargs["repo"], st.call_args.kwargs["logs_dir"]),
                         ((self.lane,), ROOT, self.ws / "logs"))

    def test_a_lane_that_does_not_start_again_fails_the_run_and_the_rerun_starts_it(self):
        from _helpers import bridge_lease
        proc = bridge_lease.start(self.ws, self.lane)
        self.addCleanup(bridge_lease.stop, proc)
        _calls, start = self.started({"ok": False, "reason": "no_token"})
        out, why = self.migrate(procs={}, start=start)
        self.assertEqual(out["lane"], f"stopped pid {proc.pid}; it did not start again (no_token)")
        self.assertIn(f"run connections.py start {self.lane}", why)
        self.assertEqual(C.worker_dirs(W, self.ws), (self.own / "tasks", self.own / "results"),
                         "the switch itself is kept")
        calls, start = self.started({"ok": True, "lease": "held", "pid": 9})
        out, why = self.migrate(procs={}, start=start)
        self.assertEqual((why, out["lane"]), ("", "the lane was down; started again (pid 9)"))
        self.assertEqual(len(calls), 1)
        calls, start = self.started({"ok": True, "lease": "held", "pid": 10})
        out, why = self.migrate(procs={}, start=start)
        self.assertEqual((why, out["lane"], calls), ("", "not-running", []), "nothing owed: a down lane is left")

    def test_a_start_that_raises_is_reported_by_its_type_and_owed(self):
        from _helpers import bridge_lease
        proc = bridge_lease.start(self.ws, self.lane)
        self.addCleanup(bridge_lease.stop, proc)

        def start(instance, ws):
            raise RuntimeError("boom")
        out, why = self.migrate(procs={}, start=start)
        self.assertEqual(out["lane"], f"stopped pid {proc.pid}; it did not start again (RuntimeError)")
        self.assertIn("re-run to retry", why)

    def test_a_run_killed_between_the_stop_and_the_start_leaves_the_restart_owed(self):
        from _helpers import bridge_lease
        proc = bridge_lease.start(self.ws, self.lane)
        self.addCleanup(bridge_lease.stop, proc)

        def killed(instance, ws):
            raise KeyboardInterrupt("the run dies here")
        with self.assertRaises(KeyboardInterrupt):
            self.migrate(procs={}, start=killed)
        owed = worker_lane._restart_owed(W, self.ws)
        self.assertTrue(owed.exists(), "owed before the stop, so it outlives the run")
        calls, start = self.started({"ok": True, "lease": "held", "pid": 11})
        out, why = self.migrate(procs={}, start=start)
        self.assertEqual((why, out["lane"], len(calls)), ("", "the lane was down; started again (pid 11)", 1))
        self.assertFalse(owed.exists(), "cleared once the lane is running again")

    def test_a_verified_restart_clears_an_owed_restart(self):
        from _helpers import bridge_lease
        proc = bridge_lease.start(self.ws, self.lane)
        self.addCleanup(bridge_lease.stop, proc)
        worker_lane._owe_restart(W, self.ws, self.lane, "stopped_for_relaunch")
        _calls, start = self.started({"ok": True, "lease": "held", "pid": 12})
        self.assertEqual(self.migrate(procs={}, start=start)[1], "")
        self.assertFalse(worker_lane._restart_owed(W, self.ws).exists())

    def test_a_disabled_lane_clears_an_owed_restart_without_starting(self):
        worker_lane._owe_restart(W, self.ws, self.lane, "stopped_for_relaunch")
        self.edit_worker_row(disabled=True)
        calls, start = self.started({"ok": True, "lease": "held", "pid": 13})
        self.assertEqual(worker_lane._stop_unless_declared(W, self.ws, None, {}, start), ("not-running", 0, ""))
        self.assertEqual(calls, [])
        self.assertFalse(worker_lane._restart_owed(W, self.ws).exists())

    def test_the_cli_reports_a_lane_left_down_as_not_ok(self):
        from _helpers import bridge_lease
        proc = bridge_lease.start(self.ws, self.lane)
        self.addCleanup(bridge_lease.stop, proc)
        with mock.patch.object(worker_lane, "_start_lane", return_value={"ok": False, "reason": "no_token"}):
            rc, said = self.cli("migrate-dirs", "--worker", W, "--apply")
        line = json.loads(said)
        self.assertEqual((rc, line["ok"]), (1, False))
        self.assertIn("did not start again (no_token)", line["why"])

    def test_undo_starts_the_lane_again_too(self):
        from _helpers import bridge_lease
        self.migrate()
        proc = bridge_lease.start(self.ws, self.lane)
        self.addCleanup(bridge_lease.stop, proc)
        calls, start = self.started({"ok": True, "lease": "held", "pid": 8})
        out, why = self.migrate(undo=True, procs={}, start=start)
        self.assertEqual((why, out["lane"]), ("", f"stopped pid {proc.pid}; started again (pid 8)"))
        self.assertEqual(calls[0][2], None, "started after the field was cleared")

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
        clock, slept = iter(range(0, 1000, 100)), []
        with mock.patch.object(C, "bridge_pid", return_value=4242), \
                mock.patch.object(worker_lane, "held_by", return_value="declared-bridge"):
            self.assertIn("pending", worker_lane.relaunch_lane(W, self.ws, procs={}, wait_s=250,
                                                               sleep=slept.append, clock=lambda: next(clock))[1])
            self.assertEqual(len(slept), 2)
            real = worker_lane.relaunch_lane
            with mock.patch.object(worker_lane, "relaunch_lane",
                                   lambda *a, **k: real(*a, **k, wait_s=0, sleep=lambda s: None)):
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
        rc, said = self.cli("migrate-dirs", "--worker", W, "--apply")
        line = json.loads(said)
        self.assertEqual((rc, line["ok"], line["undo"], line["applied"]), (0, True, False, True))
        rc, said = self.cli("migrate-dirs", "--worker", W, "--undo", "--apply")
        self.assertEqual((rc, json.loads(said)["undo"]), (0, True))

    def test_a_refusal_exits_1_with_why(self):
        self.edit_worker_row(agent_mxid="")
        rc, said = self.cli("migrate-dirs", "--worker", W, "--apply")
        self.assertEqual((rc, json.loads(said)["ok"]), (1, False))
        self.assertIn("why", json.loads(said))

    def test_without_apply_it_only_shows_what_it_would_do(self):
        self.deliver(self.core)
        (self.ws / "tasks" / f"{self.mine}.txt").write_text("t")
        (self.ws / "results").mkdir(parents=True)
        stamped = proactive_filename(1, "ag2space", W)
        for name in (f"{self.mine}.txt", "task-other.txt", stamped):
            (self.ws / "results" / name).write_text("r")
        before = sorted(str(p.relative_to(self.ws)) for p in self.ws.rglob("*") if p.is_file())
        rc, said = self.cli("migrate-dirs", "--worker", W)
        line = json.loads(said)
        self.assertEqual((rc, line["applied"], line["own_now"], line["lane"], line["delivered"]),
                         (1, False, False, "not-running", [self.core]))
        self.assertIn("no task-event handler can move them", line["why"])
        self.assertEqual(line["would_move_tasks"], [f"{self.mine}.txt"])
        self.assertEqual(sorted(line["would_move_results"]), sorted([f"{self.mine}.txt", stamped]))
        self.assertEqual(sorted(str(p.relative_to(self.ws)) for p in self.ws.rglob("*") if p.is_file()), before)
        self.assertIsNone(C.worker_dirs(W, self.ws))
        self.declare_handler()
        rc, said = self.cli("migrate-dirs", "--worker", W)
        self.assertEqual((rc, json.loads(said)["ok"]), (0, True))

    def test_the_undo_preview_lists_what_would_return(self):
        self.migrate()
        (self.own / "tasks" / f"{self.mine}.txt").write_text("t")
        rc, said = self.cli("migrate-dirs", "--worker", W, "--undo")
        line = json.loads(said)
        self.assertEqual((rc, line["own_now"], line["would_return_tasks"]), (0, True, [f"{self.mine}.txt"]))

    def test_a_preview_refuses_without_an_agent_or_a_readable_folder(self):
        from delivery import task_dispatch
        with mock.patch.object(task_dispatch, "owned_task_ids", side_effect=task_dispatch.WorkerHoldUnreadable("io")):
            self.assertEqual(worker_lane.preview_migrate(W, self.ws)[1], "io")
        with mock.patch.object(C, "bridge_pid", side_effect=C.BridgeLeaseIndeterminate(self.lane, "torn")):
            self.assertEqual(worker_lane.preview_migrate(W, self.ws, undo=True)[0]["lane"], "unknown")
        with mock.patch.object(worker_lane, "handler_lookup", return_value=("", "lookup failed")):
            self.assertIn("nothing would be switched", worker_lane.preview_migrate(W, self.ws)[1])
        self.edit_worker_row(agent_mxid="")
        self.assertIn("no agent identity row", worker_lane.preview_migrate(W, self.ws)[1])

    def test_the_live_txt_listing_survives_an_unreadable_folder(self):
        with mock.patch.object(Path, "glob", side_effect=OSError):
            self.assertEqual(worker_lane._live_txt(self.ws), [])



class SupervisedSwitch(Base):
    """A switch with the worker's seat running, read back through production supervision and routing."""

    def setUp(self):
        super().setUp()
        import pool_roster as pr
        import worker_identity as wi
        self.sock = str(self.ws / "tmux.sock")
        wi.create_worker(self.ws, runtime="claude", tmux_socket=self.sock, worker_id=W, cwd=str(ROOT))
        pr.register_worker(self.ws, W, "reviewer", runtime="claude")
        seat = wi.tmux_session_name(W)
        self.tmux = FakeTmux({seat, seat + "-watcher", seat + "-input"})
        self.handler = str(SCRIPTS / "pool_route_handler.py")

    def test_accepted_legacy_work_keeps_the_worker_on_its_old_inbox_for_reseat_and_retire(self):
        import pool_manage as pm
        import pool_supervise as sup
        self.deliver(self.core, ".accepted")
        out, why = self.migrate(handler=self.handler)
        spawned = []
        reseat = pm.reseat(self.ws, ROOT, W, runner=self.tmux, spawn=lambda *a, **kw: spawned.append(kw) or {"runtime_session_id": None})
        self.assertEqual((reseat["result"], reseat.get("owed"), spawned), ("deferred", [self.core], []))
        with self.assertRaises(pm.Refused):
            pm.retire(self.ws, W, runner=self.tmux)
        self.assertEqual(sup.owed_task_ids(self.ws, W), [self.core])
        self.assertTrue((pd.deliveries_dir(self.ws, W) / f"{self.core}.accepted").is_file())
        self.assertIsNone(C.worker_dirs(W, self.ws), "its seat and lane stay on the shared folders")
        self.assertIn("accepted are still in its deliveries folder", why)
        self.assertEqual(out["state"], "migrating")
        # Once answered, a re-run finishes the switch.
        (self.ws / "results").mkdir(parents=True, exist_ok=True)
        (self.ws / "results" / f"{self.core}.txt").write_text("answer")
        out, why = self.migrate(handler=self.handler)
        self.assertEqual((why, out["state"]), ("", "own"))

    def test_owed_work_left_in_deliveries_after_a_switch_stays_visible(self):
        import pool_supervise as sup
        self.deliver(self.core, ".accepted")
        C.set_worker_dirs(W, True, workspace=self.ws)
        self.assertEqual(sup.owed_task_ids(self.ws, W), [self.core])

    def test_a_route_that_chose_the_worker_before_the_switch_does_not_deliver_into_its_old_folder(self):
        import pool_router as rt
        task = {"id": self.core, "requested_worker": W}
        (self.ws / "tasks").mkdir(parents=True, exist_ok=True)
        (self.ws / "tasks" / f"{self.core}.txt").write_text(f"id: {self.core}\ntask: x\n")
        real, switched = rt.own_dir_workers, []

        def decided_then_switched(workspace, targets):
            chosen = real(workspace, targets)
            if not switched:
                switched.append(self.migrate(handler=self.handler))
            return chosen
        with mock.patch.object(rt, "own_dir_workers", decided_then_switched):
            out = rt.route(self.ws, task)
        self.assertEqual(switched[0][1], "")
        self.assertIsNotNone(C.worker_dirs(W, self.ws))
        self.assertIsNone(pd.find(self.ws, W, self.core), "nothing lands in the folder the switch emptied")
        self.assertIsNone(pat.worker_for_task(self.ws, self.core))
        self.assertEqual((out["delivered"], out["fenced"]), ([], [W]))

    def test_while_migrating_a_core_task_is_fenced_and_its_own_lane_task_is_delivered(self):
        import pool_router as rt
        C.set_worker_dirs(W, C.WORKER_DIRS_MIGRATING, workspace=self.ws)
        (self.ws / "tasks").mkdir(parents=True, exist_ok=True)
        for t in (self.core, self.mine):
            (self.ws / "tasks" / f"{t}.txt").write_text(f"id: {t}\ntask: x\n")
        self.assertEqual(rt.deliver_one(self.ws, W, self.core), "fenced")
        self.assertEqual(rt.deliver_one(self.ws, W, self.mine), "delivered")


class FakeTmux:
    def __init__(self, alive=()):
        self.alive = set(alive)

    def __call__(self, argv, **kw):
        if argv[:1] == ["tmux"] and "has-session" in argv:
            name = argv[argv.index("-t") + 1].lstrip("=")
            return subprocess.CompletedProcess(argv, 0 if name in self.alive else 1, "", "")
        if argv[:1] == ["tmux"] and "kill-session" in argv:
            self.alive.discard(argv[argv.index("-t") + 1].lstrip("="))
        return subprocess.CompletedProcess(argv, 0, "", "")


if __name__ == "__main__":
    unittest.main()
