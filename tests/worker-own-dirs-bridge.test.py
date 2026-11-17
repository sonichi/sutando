#!/usr/bin/env python3
"""The bridge side of a converted pool worker's own folders (`dirs: "own"`).

- `worker_lane.lane_dirs` / `bridge_overlay`: the worker's lane runs on `<ws>/agents/<segment>/{tasks,results}`;
  a shared-dir worker's lane, a core lane and the primary stay on the shared tasks/ and results/.
- `install(own=True)`: TASK_ADDRESSEE and the claim gate as before, no router, an attribution-only
  pre-publish hook (`--attribute-only --worker W`), and the adopt thread that moves the worker's stamped
  proactive files out of the shared results/.
- The lane loader and the declared bridge both end on the own folders with the same hook set.
- `pool_route_handler --attribute-only`: records the task as the worker's, idempotently, never routing it.

Run: python3 tests/worker-own-dirs-bridge.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
_spec = importlib.util.spec_from_file_location("worker_lane_bridge_suite", ROOT / "tests" / "worker-lane-sparrow-bridge.test.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)
C, W, bridge, worker_lane = S.C, S.W, S.bridge, S.worker_lane
sys.path.insert(0, str(ROOT / "skills" / "worker-pool" / "scripts"))
import pool_attribution as pat, pool_route_handler as prh  # noqa: E401,E402

from proactive_routing import proactive_filename  # noqa: E402

OTHER = "8d811f22" + "0" * 24


class Own(S.Base):
    def setUp(self):
        super().setUp()
        self.own = self.agents / S.SEG
        C.set_worker_dirs(W, True, workspace=self.ws)


class LaneDirs(Own):
    def test_the_workers_lane_is_on_its_own_folders(self):
        lane = self.row["instance"]
        self.assertEqual(worker_lane.lane_dirs(self.ws, lane), (self.own / "tasks", self.own / "results", True))
        overlay = worker_lane.bridge_overlay(self.ws, lane)
        self.assertEqual((overlay["AGENT_CONNECT_TASK_DIR"], overlay["AGENT_CONNECT_RESULT_DIR"],
                          overlay["AGENT_CONNECT_STATE_DIR"]),
                         (str(self.own / "tasks"), str(self.own / "results"), str(self.ws / "state")))

    def test_shared_for_a_shared_worker_a_core_lane_and_the_primary(self):
        shared = (self.ws / "tasks", self.ws / "results", False)
        self.assertEqual(worker_lane.lane_dirs(self.ws, ""), shared)
        self.assertEqual(worker_lane.lane_dirs(self.ws, "alpha-fixture-net"), shared)
        C.set_worker_dirs(W, False, workspace=self.ws)
        self.assertEqual(worker_lane.lane_dirs(self.ws, self.row["instance"]), shared)


class InstallOwn(Own):
    def install(self, handler=True):
        if handler:
            self.declare_handler()
        ns, logged = {}, []
        worker_lane.install(ns, W, workspace=self.ws, results_dir=self.own / "results", log=logged.append,
                            who="b1", own=True, adopt_interval_s=3600)
        self.addCleanup(ns["WORKER_ADOPT_STOP"].set)
        return ns, logged

    def test_no_router_only_attribution(self):
        ns, logged = self.install()
        self.assertEqual(ns["TASK_ADDRESSEE"](), W)
        self.assertIs(ns["PROACTIVE_CLAIM_GATE"], ns["PROACTIVE_OWN_FILE"])
        self.assertEqual(ns["TASK_PRE_PUBLISH"].__qualname__, "attributor.<locals>.attribute")
        self.assertIn("on its own folders", logged[0])

    def test_without_a_handler_nothing_is_attributed(self):
        ns, logged = self.install(handler=False)
        self.assertNotIn("TASK_PRE_PUBLISH", ns)
        self.assertTrue(any("not attributed" in line for line in logged), logged)

    def test_the_attributor_runs_the_handler_and_never_holds_the_task(self):
        logged = []
        hook = worker_lane.attributor(W, ["handler", "--from-lane"], logged.append)
        seen = []
        with mock.patch.object(subprocess, "run", side_effect=lambda argv, **kw: seen.append(argv)
                               or subprocess.CompletedProcess(argv, 0)):
            self.assertIsNone(hook("t1", Path("/s"), Path("/d")))
        self.assertEqual(seen, [["handler", "--from-lane", "--task-file", "/d", "--payload", "/s",
                                 "--attribute-only", "--worker", W]])
        self.assertEqual(logged, [])
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            self.assertIsNone(hook("t2", Path("/s"), Path("/d")))
        with mock.patch.object(subprocess, "run", side_effect=OSError("gone")):
            self.assertIsNone(hook("t3", Path("/s"), Path("/d")))
        self.assertEqual([line.split(":")[1].split()[0] for line in logged], ["t2", "t3"])


class Adopt(Own):
    def test_moves_only_its_workers_stamped_files(self):
        shared, own = self.ws / "results", self.own / "results"
        shared.mkdir(parents=True, exist_ok=True)
        mine, theirs, core = (proactive_filename(1, "ag2space", W), proactive_filename(2, "ag2space", OTHER),
                              "proactive-3.txt")
        for name in (mine, theirs, core, "task-1.txt"):
            (shared / name).write_text("x")
        self.assertEqual(worker_lane.adopt_stray_proactive(W, shared, own), [mine])
        self.assertTrue((own / mine).exists())
        self.assertEqual(sorted(p.name for p in shared.iterdir()), sorted([theirs, core, "task-1.txt"]))
        self.assertEqual(worker_lane.adopt_stray_proactive("", shared, own), [])

    def test_a_name_already_in_its_folder_or_a_lost_race_is_left(self):
        shared, own = self.ws / "results", self.own / "results"
        shared.mkdir(parents=True, exist_ok=True)
        own.mkdir(parents=True)
        mine = proactive_filename(1, "ag2space", W)
        (shared / mine).write_text("new")
        (own / mine).write_text("old")
        self.assertEqual(worker_lane.adopt_stray_proactive(W, shared, own), [])
        (own / mine).unlink()
        with mock.patch.object(os, "rename", side_effect=FileNotFoundError):
            self.assertEqual(worker_lane.adopt_stray_proactive(W, shared, own), [])
        self.assertEqual(worker_lane.adopt_stray_proactive(W, self.ws / "missing", own), [])

    def test_the_loop_logs_each_move_and_stops(self):
        shared, own = self.ws / "results", self.own / "results"
        shared.mkdir(parents=True, exist_ok=True)
        mine = proactive_filename(1, "ag2space", W)
        (shared / mine).write_text("x")
        logged, stop = [], threading.Event()
        stop.set()
        worker_lane._adopt_loop(W, shared, own, logged.append, stop, 0)
        self.assertEqual(len(logged), 1)
        self.assertIn(mine, logged[0])

    def test_the_glob_failing_moves_nothing(self):
        with mock.patch.object(Path, "glob", side_effect=OSError):
            self.assertEqual(worker_lane.adopt_stray_proactive(W, self.ws, self.own), [])


class LoaderParity(Own):
    def test_the_lane_and_the_declared_bridge_run_on_the_own_folders(self):
        self.declare_handler()
        lane = self.row["instance"]
        env = C.launch_env(C.plan_entry(lane, {}, workspace=self.ws), {}, vault=self.vault)
        parity = S.TheHookParity("test_the_lane_and_the_declared_bridge_install_the_same_hooks")
        parity.ws, parity.vault, parity.addCleanup = self.ws, self.vault, self.addCleanup

        def as_lane(run_path):
            with mock.patch.dict(os.environ, env), mock.patch.object(worker_lane, "ADOPT_INTERVAL_S", 3600):
                return run_path(str(ROOT / "src" / "remote-gateway-bridge.py"), "__main__")
        lane_ns = parity.loaded(as_lane)
        decl = self.declare()

        def as_bridge(run_path):
            out = {}
            with mock.patch.object(worker_lane, "ADOPT_INTERVAL_S", 3600):
                rc = bridge.main([str(decl)], workspace=self.ws, get_key=lambda k: self.fail(k), vault=self.vault,
                                 run=lambda *a, **k: self.fail("the bare package ran"),
                                 run_path=lambda path, run_name: out.update(run_path(path, run_name)))
            self.assertEqual(rc, 0)
            return out
        bridge_ns = parity.loaded(as_bridge)
        for ns in (lane_ns, bridge_ns):
            self.addCleanup(ns["WORKER_ADOPT_STOP"].set)
            self.assertEqual((ns["TASKS_DIR"], ns["RESULTS_DIR"]), (self.own / "tasks", self.own / "results"))
            self.assertEqual(ns["TASK_PRE_PUBLISH"].__qualname__, "attributor.<locals>.attribute")
        self.assertEqual({k: S._shape(bridge_ns[k]) for k in S.HOOKS}, {k: S._shape(lane_ns[k]) for k in S.HOOKS})


class AttributeOnly(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = tmp.name

    def run_handler(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()):
            return prh.main(["--task-file", f"{self.ws}/tasks/task-l~1.txt", "--workspace", self.ws,
                             "--from-lane", "--attribute-only", *argv])

    def test_records_once_and_is_idempotent(self):
        self.assertEqual(self.run_handler("--worker", W), 0)
        self.assertEqual(pat.worker_for_task(self.ws, "task-l~1"), W)
        self.assertEqual(self.run_handler("--worker", W), 0)

    def test_refuses_another_worker_or_a_non_worker(self):
        self.assertEqual(self.run_handler("--worker", W), 0)
        self.assertEqual(self.run_handler("--worker", OTHER), 1)
        self.assertEqual(self.run_handler("--worker", "core"), 1)
        self.assertEqual(self.run_handler(), 1)


if __name__ == "__main__":
    unittest.main()
