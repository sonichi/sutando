#!/usr/bin/env python3
"""A worker's own lane delivers each task it queues to its worker, without waiting for the core.

- `_install_worker_lane` makes the core's declared task-event handler (the pool router) the lane's
  wake command, with `--from-lane`; with no handler declared, or a wake command of its own, it
  changes nothing and the core's watcher routes as before.
- One queued task, end to end through the real handler: one sentinel in the worker's delivery
  folder and the attribution beside it, the payload where readers resolve it, no watcher receipt.
  The watcher's own later pass finds it committed and delivers nothing twice.
- A worker no longer on the roster gets nothing from its lane: the core's watcher decides.

Run: python3 tests/worker-lane-native-pickup.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import importlib.util
import json
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "skills" / "worker-pool" / "scripts"))

import pool_attribution as pa
import pool_roster
import pool_route_handler as handler
import pool_routing_receipt as prr
import resolve_inbox_entry
from task_disposition import task_disposition

W = "d" * 32
OTHER = "e" * 32


def _load_shim():
    spec = importlib.util.spec_from_file_location("rgb_native_pickup", ROOT / "src" / "remote-gateway-bridge.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["rgb_native_pickup"] = m
    spec.loader.exec_module(m)
    return m


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rgb = _load_shim()

    def setUp(self):
        rgb = self.rgb
        self.ws = Path(tempfile.mkdtemp(prefix="worker-lane-pickup-"))
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        for d in ("tasks", "results", "state", "deliveries"):
            (self.ws / d).mkdir()
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live", "label": "fast"}}, "bindings": {}}))
        self.saved = {k: getattr(rgb, k) for k in ("WS", "RESULTS_DIR", "TASK_ADDRESSEE", "PROACTIVE_CLAIM_GATE",
                                                   "PROACTIVE_OWN_FILE", "ON_TASK_CMD", "_log")}
        rgb.WS, rgb.RESULTS_DIR, rgb.ON_TASK_CMD = self.ws, self.ws / "results", ""
        self.logged = []
        rgb._log = self.logged.append

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.rgb, k, v)

    def declare_handler(self):
        pool_roster.publish_task_event_handler(self.ws)

    def queue(self, tid, worker):
        path = self.ws / "tasks" / f"{tid}.txt"
        path.write_text(f"id: {tid}\nsource: ag2space\nchannel_id: !room:alpha-fixture.net\n"
                        f"requested_worker: {worker}\ntask: hello\n")
        return path

    def sentinels(self, worker):
        folder = self.ws / "deliveries" / worker
        return sorted(p.name for p in folder.iterdir() if p.is_file() and p.name.startswith("task-")) \
            if folder.is_dir() else []


class TheWakeCommand(Base):
    def test_a_worker_lane_runs_the_declared_handler_from_the_lane(self):
        self.declare_handler()
        self.rgb._install_worker_lane(W)
        argv = shlex.split(self.rgb.ON_TASK_CMD)
        self.assertEqual(Path(argv[0]).name, "pool_route_handler.py")
        self.assertEqual(argv[1:], ["--workspace", str(self.ws), "--results-dir", str(self.ws / "results"),
                                    "--from-lane", "--task-file"])

    def test_no_declared_handler_leaves_routing_to_the_cores_watcher(self):
        self.rgb._install_worker_lane(W)
        self.assertEqual(self.rgb.ON_TASK_CMD, "")
        self.assertTrue(any("no task-event handler" in line for line in self.logged), self.logged)

    def test_a_handler_lookup_that_cannot_run_leaves_routing_to_the_cores_watcher(self):
        import subprocess
        from unittest import mock
        self.declare_handler()
        for exc in (OSError("no bash"), subprocess.TimeoutExpired("bash", 10)):
            with self.subTest(exc=type(exc).__name__), mock.patch.object(subprocess, "run", side_effect=exc):
                self.rgb.ON_TASK_CMD = ""
                self.rgb._install_worker_lane(W)
                self.assertEqual(self.rgb.ON_TASK_CMD, "")

    def test_the_lane_says_whose_agent_it_runs_as(self):
        self.rgb._install_worker_lane(W)
        self.assertTrue(any(f"runs as worker {W}'s own agent" in line for line in self.logged), self.logged)

    def test_a_lanes_own_wake_command_is_kept(self):
        self.declare_handler()
        self.rgb.ON_TASK_CMD = "my-agent run-task"
        self.rgb._install_worker_lane(W)
        self.assertEqual(self.rgb.ON_TASK_CMD, "my-agent run-task")


class OneQueuedTask(Base):
    def fire(self, tid, worker):
        self.declare_handler()
        self.rgb._install_worker_lane(worker)
        path = self.queue(tid, worker)
        self.rgb._fire_on_task(tid, path)
        return path

    def wait_for(self, check, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.05)
        return False

    def test_the_worker_gets_one_delivery_without_the_core(self):
        tid = "task-lane-1"
        payload = self.fire(tid, W)
        self.assertTrue(self.wait_for(lambda: pa.attribution_path(self.ws, tid).is_file()), self.logged)
        self.assertEqual(self.sentinels(W), [f"{tid}.txt"])
        self.assertEqual(pa.attribution_path(self.ws, tid).read_text().strip(), W)
        self.assertEqual(resolve_inbox_entry.resolve(str(self.ws / "deliveries" / W / f"{tid}.txt"), self.ws),
                         payload)
        self.assertEqual(task_disposition(self.ws, tid), "pending")
        self.assertIsNone(prr.read(self.ws), "a lane's run is not the watcher's receipt")
        rc = handler.guarded_main(["--task-file", str(payload), "--workspace", str(self.ws),
                                   "--results-dir", str(self.ws / "results")])
        self.assertEqual(rc, 0, "the watcher's pass finds it committed")
        self.assertEqual(self.sentinels(W), [f"{tid}.txt"], "and delivers nothing twice")
        self.assertEqual(prr.read(self.ws)["task_id"], tid)

    def test_a_worker_off_the_roster_gets_nothing_from_its_lane(self):
        tid = "task-lane-2"
        self.fire(tid, OTHER)
        self.assertTrue(self.wait_for(lambda: any("on-task command exited" in line for line in self.logged)),
                        self.logged)
        self.assertEqual(self.sentinels(OTHER), [])
        self.assertEqual(self.sentinels(W), [])
        self.assertFalse(pa.attribution_path(self.ws, tid).exists())


if __name__ == "__main__":
    unittest.main()
