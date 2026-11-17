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


class TheDeclaredBridgesDirs(Own):
    def run_bridge(self):
        import workspace_default
        from ag2_sparrow import _dirs
        decl, seen = self.declare(), []
        real = _dirs.set_dirs
        with mock.patch.object(_dirs, "set_dirs", side_effect=lambda **kw: (seen.append(kw), real(**kw))), \
                mock.patch.object(workspace_default, "resolve_workspace", return_value=self.ws):
            rc = bridge.main([str(decl)], workspace=self.ws, get_key=lambda k: self.fail(k), vault=self.vault,
                             run=lambda *a, **k: self.fail("the bare package ran"), run_path=lambda path, run_name: None)
        self.assertEqual(rc, 0)
        return seen[0]

    def test_the_merged_dirs_it_runs_with_are_the_own_folders(self):
        self.assertEqual(self.run_bridge(), {"task_dir": str(self.own / "tasks"), "result_dir": str(self.own / "results"),
                                             "state_dir": str(self.ws / "state")})

    def test_a_shared_dir_worker_runs_on_the_shared_folders(self):
        C.set_worker_dirs(W, False, workspace=self.ws)
        self.assertEqual(self.run_bridge()["task_dir"], str(self.ws / "tasks"))

    def test_the_declaration_strips_the_rows_agent(self):
        self.edit_worker_row(agent_mxid=f" {S.WORKER_AGENT} ")
        decl, why = worker_lane._declaration_for(W, self.ws)
        self.assertEqual((decl["agent"], why), (S.WORKER_AGENT, ""))
        self.assertEqual(C.worker_agents_dir(self.ws), self.agents)


class TheFolderContract(Own):
    def test_the_agent_root_is_private_and_holds_both_folders(self):
        import stat
        self.own.mkdir(parents=True, mode=0o755)
        os.chmod(self.own, 0o755)
        worker_lane.lane_dirs(self.ws, self.row["instance"])
        self.assertEqual(stat.S_IMODE(self.own.stat().st_mode), 0o700)
        for d in ("tasks/archive", "results/archive"):
            self.assertTrue((self.own / d).is_dir(), d)
        self.assertIsNone(C.ensure_worker_dirs(OTHER, self.ws))

    def test_a_second_worker_row_naming_the_agent_refuses_own_folders(self):
        import json
        path = C.record_path(self.ws)
        rec = json.loads(path.read_text())
        mine = next(r for r in rec["connections"] if r.get("worker") == W)
        rec["connections"].append(dict(mine, worker=OTHER, instance="alpha-fixture-net-8d811f22",
                                       token_ref="AG2_REMOTE_TOKEN_TWIN", server="beta-fixture.net"))
        mine.pop("dirs", None)
        path.write_text(json.dumps(rec))
        with self.assertRaises(C.ConnectionsError) as caught:
            C.set_worker_dirs(W, True, workspace=self.ws)
        self.assertIn(f"worker {OTHER}'s row also names agent", str(caught.exception))
        self.assertIsNone(C.worker_dirs(W, self.ws))
        self.assertNotIn("dirs", C.set_worker_dirs(W, False, workspace=self.ws))


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
        self.assertEqual(((shared / mine).read_text(), (own / mine).read_text()), ("new", "old"))
        (own / mine).unlink()
        import undelivered_quarantine
        with mock.patch.object(undelivered_quarantine, "rename_noreplace", side_effect=FileNotFoundError):
            self.assertEqual(worker_lane.adopt_stray_proactive(W, shared, own), [])
        self.assertEqual(worker_lane.adopt_stray_proactive(W, self.ws / "missing", own), [])

    def test_a_file_published_in_its_folder_just_before_the_move_survives(self):
        import undelivered_quarantine
        shared, own = self.ws / "results", self.own / "results"
        shared.mkdir(parents=True, exist_ok=True)
        mine = proactive_filename(1, "ag2space", W)
        (shared / mine).write_text("stray")
        real = undelivered_quarantine.rename_noreplace

        def racing(src, dst, log=None):
            Path(dst).write_text("published")
            return real(src, dst, log)
        with mock.patch.object(undelivered_quarantine, "rename_noreplace", racing):
            self.assertEqual(worker_lane.adopt_stray_proactive(W, shared, own), [])
        self.assertEqual(((own / mine).read_text(), (shared / mine).read_text()), ("published", "stray"))

    def test_without_a_no_replace_rename_nothing_moves_and_a_republished_source_survives(self):
        import errno
        import undelivered_quarantine
        shared, own = self.ws / "results", self.own / "results"
        shared.mkdir(parents=True, exist_ok=True)
        mine = proactive_filename(1, "ag2space", W)
        (shared / mine).write_text("stray")
        nothing = FileExistsError(errno.ENOTSUP, "no primitive")

        def republish(src, dst, log=None):
            tmp = Path(src).with_name(".republish")
            tmp.write_text("republished")
            os.replace(tmp, src)
            raise nothing
        logged = []
        undelivered_quarantine._REPORTED.discard("worker-lane:adopt")
        self.addCleanup(undelivered_quarantine._REPORTED.discard, "worker-lane:adopt")
        with mock.patch.object(undelivered_quarantine, "rename_noreplace", republish):
            self.assertEqual(worker_lane.adopt_stray_proactive(W, shared, own, logged.append), [])
            self.assertEqual(worker_lane.adopt_stray_proactive(W, shared, own, logged.append), [])
        self.assertEqual((shared / mine).read_text(), "republished")
        self.assertFalse((own / mine).exists())
        self.assertEqual(len(logged), 1)
        self.assertIn("no no-replace rename", logged[0])

    def test_its_lanes_task_results_move_and_others_stay(self):
        shared, own = self.ws / "results", self.own / "results"
        shared.mkdir(parents=True, exist_ok=True)
        lane, core = f"task-{self.row['instance']}~a1.txt", "task-1700000000000.txt"
        for name in (lane, core, "task-alpha-fixture-net~b2.txt"):
            (shared / name).write_text("r")
        self.assertEqual(worker_lane.adopt_stray_results(W, self.ws, own), [lane])
        self.assertTrue((own / lane).is_file())
        self.assertEqual(sorted(p.name for p in shared.glob("*.txt")), sorted([core, "task-alpha-fixture-net~b2.txt"]))
        self.assertEqual(worker_lane.adopt_stray_results("", self.ws, own), [])
        self.assertEqual(worker_lane.adopt_stray_proactive("", shared, own), [])

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
