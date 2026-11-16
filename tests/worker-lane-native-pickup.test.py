#!/usr/bin/env python3
"""A worker's own lane delivers each task it queues to its worker before the core can see it.

- `_install_worker_lane` makes the core's declared task-event handler (the pool router) the lane's
  pre-publish step, with `--from-lane --payload <staged copy>`; with no handler declared it changes
  nothing and the core's watcher routes as before. A lane's own wake command is left alone.
- One queued task, end to end through the real handler and the bridge's publish: the sentinel and
  attribution are written BEFORE the payload reaches `tasks/`, so the core's watcher finds it held
  (and skips it); the payload is where every reader resolves it; no watcher receipt.
- A worker off the roster or retired gets nothing from its lane: the task is published unheld and
  the core's watcher routes it. A router that fails or hangs never holds the task back.

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
from delivery.task_dispatch import worker_holds
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
                                                   "PROACTIVE_OWN_FILE", "ON_TASK_CMD", "TASK_PRE_PUBLISH",
                                                   "_log")}
        rgb.WS, rgb.RESULTS_DIR, rgb.ON_TASK_CMD, rgb.TASK_PRE_PUBLISH = self.ws, self.ws / "results", "", None
        self.logged = []
        rgb._log = self.logged.append

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.rgb, k, v)

    def declare_handler(self):
        pool_roster.publish_task_event_handler(self.ws)

    def stage(self, tid, worker, source="ag2space"):
        """The bridge's staged copy beside its destination, as `_stage_durable` leaves it."""
        staged = self.ws / "tasks" / f".{tid}.txt.abc123.tmp"
        staged.write_text(f"id: {tid}\nsource: {source}\nchannel_id: !room:alpha-fixture.net\n"
                          f"requested_worker: {worker}\ntask: hello\n")
        return staged, self.ws / "tasks" / f"{tid}.txt"

    def sentinels(self, worker):
        folder = self.ws / "deliveries" / worker
        return sorted(p.name for p in folder.iterdir() if p.is_file() and p.name.startswith("task-")) \
            if folder.is_dir() else []


class TheLaneRouter(Base):
    def test_a_worker_lane_routes_through_the_declared_handler_from_the_lane(self):
        self.declare_handler()
        self.rgb._install_worker_lane(W)
        self.assertIsNotNone(self.rgb.TASK_PRE_PUBLISH)
        self.assertEqual(self.rgb.ON_TASK_CMD, "", "no wake command is borrowed for routing")
        argv = self.rgb._lane_route_argv()
        self.assertEqual(Path(argv[0]).name, "pool_route_handler.py")
        self.assertEqual(argv[1:], ["--workspace", str(self.ws), "--results-dir", str(self.ws / "results"),
                                    "--from-lane"])

    def test_no_declared_handler_leaves_routing_to_the_cores_watcher(self):
        self.rgb._install_worker_lane(W)
        self.assertIsNone(self.rgb.TASK_PRE_PUBLISH)
        self.assertTrue(any("no task-event handler" in line for line in self.logged), self.logged)

    def test_a_handler_lookup_that_cannot_run_leaves_routing_to_the_cores_watcher(self):
        import subprocess
        from unittest import mock
        self.declare_handler()
        for exc in (OSError("no bash"), subprocess.TimeoutExpired("bash", 10)):
            with self.subTest(exc=type(exc).__name__), mock.patch.object(subprocess, "run", side_effect=exc):
                self.rgb._install_worker_lane(W)
                self.assertIsNone(self.rgb.TASK_PRE_PUBLISH)

    def test_the_lane_says_whose_agent_it_runs_as(self):
        self.rgb._install_worker_lane(W)
        self.assertTrue(any(f"runs as worker {W}'s own agent" in line for line in self.logged), self.logged)

    def test_a_lanes_own_wake_command_is_kept_and_the_lane_still_routes(self):
        self.declare_handler()
        self.rgb.ON_TASK_CMD = "my-agent run-task"
        self.rgb._install_worker_lane(W)
        self.assertEqual(self.rgb.ON_TASK_CMD, "my-agent run-task")
        self.assertIsNotNone(self.rgb.TASK_PRE_PUBLISH)


class OneQueuedTask(Base):
    def publish(self, tid, worker, **kw):
        """The bridge's own publish step, with this lane's router installed."""
        self.declare_handler()
        self.rgb._install_worker_lane(W)
        staged, dest = self.stage(tid, worker, **kw)
        seen = {}
        hook = self.rgb.TASK_PRE_PUBLISH

        def watching(t, s, d):
            hook(t, s, d)
            seen["held_before_publish"] = worker_holds(self.ws / "deliveries", d.name)
            seen["published_before_route"] = d.exists()
        self.rgb.TASK_PRE_PUBLISH = watching
        self.assertTrue(self.rgb._publish_task(tid, staged, dest))
        self.assertFalse(staged.exists())
        self.assertTrue(dest.is_file())
        return dest, seen

    def test_the_worker_holds_the_task_before_the_core_can_see_it(self):
        tid = "task-lane-1"
        payload, seen = self.publish(tid, W)
        self.assertFalse(seen["published_before_route"], "routed from the staged copy")
        self.assertTrue(seen["held_before_publish"], "held when it reaches tasks/: the core's watcher skips it")
        self.assertEqual(self.sentinels(W), [f"{tid}.txt"])
        self.assertEqual(pa.attribution_path(self.ws, tid).read_text().strip(), W)
        self.assertEqual(resolve_inbox_entry.resolve(str(self.ws / "deliveries" / W / f"{tid}.txt"), self.ws),
                         payload)
        self.assertEqual(task_disposition(self.ws, tid), "pending")
        self.assertIsNone(prr.read(self.ws), "a lane's run is not the watcher's receipt")
        rc = handler.guarded_main(["--task-file", str(payload), "--workspace", str(self.ws),
                                   "--results-dir", str(self.ws / "results")])
        self.assertEqual(rc, 0, "a watcher pass that runs anyway finds it committed")
        self.assertEqual(self.sentinels(W), [f"{tid}.txt"], "and delivers nothing twice")

    def test_a_replayed_publish_delivers_nothing_twice(self):
        tid = "task-lane-3"
        payload, _ = self.publish(tid, W)
        payload.unlink()
        self.publish(tid, W)
        self.assertEqual(self.sentinels(W), [f"{tid}.txt"])

    def test_a_worker_off_the_roster_gets_nothing_and_the_core_routes_it(self):
        tid = "task-lane-2"
        payload, seen = self.publish(tid, OTHER)
        self.assertFalse(seen["held_before_publish"])
        self.assertEqual(self.sentinels(OTHER) + self.sentinels(W), [])
        self.assertFalse(pa.attribution_path(self.ws, tid).exists())
        self.assertTrue(payload.is_file(), "published to the shared inbox for the core's watcher")
        self.assertTrue(any(f"{tid} not delivered to it (router exit 3)" in line for line in self.logged),
                        self.logged)

    def test_a_retired_worker_gets_nothing_and_the_core_routes_it(self):
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 2, "workers": {W: {"state": "retired", "label": "fast"}}, "bindings": {}}))
        payload, seen = self.publish("task-lane-4", W)
        self.assertFalse(seen["held_before_publish"])
        self.assertEqual(self.sentinels(W), [])
        self.assertTrue(payload.is_file())

    def test_a_picker_command_is_left_for_the_core(self):
        payload, seen = self.publish("task-lane-5", W, source="worker-picker")
        self.assertFalse(seen["held_before_publish"])
        self.assertTrue(payload.is_file())

    def test_a_router_that_fails_or_hangs_never_holds_the_task_back(self):
        import subprocess
        from unittest import mock
        for exc in (OSError("gone"), subprocess.TimeoutExpired("router", 10)):
            with self.subTest(exc=type(exc).__name__), mock.patch.object(subprocess, "run", side_effect=exc):
                route = self.rgb._lane_router(W, ["router"])
                staged, dest = self.stage("task-lane-6", W)
                route("task-lane-6", staged, dest)
                self.assertTrue(any("routing task-lane-6 failed" in line for line in self.logged), self.logged)
                self.assertEqual(self.sentinels(W), [])

    def test_a_pre_publish_hook_that_raises_still_publishes(self):
        def broken(*_):
            raise RuntimeError("boom")
        self.rgb.TASK_PRE_PUBLISH = broken
        staged, dest = self.stage("task-lane-7", W)
        self.assertTrue(self.rgb._publish_task("task-lane-7", staged, dest))
        self.assertTrue(dest.is_file())
        self.assertTrue(any("pre-publish hook failed for task-lane-7" in line for line in self.logged), self.logged)


class TheRouterOnAStagedCopy(Base):
    """The handler in-process, as the lane runs it: `--task-file` names the destination, `--payload` the
    staged bytes, which is all the router reads until the task is published."""

    def run_handler(self, tid, worker, source="ag2space"):
        staged, dest = self.stage(tid, worker, source)
        rc = handler.guarded_main(["--task-file", str(dest), "--payload", str(staged), "--workspace", str(self.ws),
                                   "--results-dir", str(self.ws / "results"), "--from-lane"])
        return rc, dest

    def test_delivers_from_the_staged_copy(self):
        rc, dest = self.run_handler("task-staged-1", W)
        self.assertEqual(rc, 0)
        self.assertFalse(dest.exists())
        self.assertEqual(self.sentinels(W), ["task-staged-1.txt"])
        self.assertIsNone(prr.read(self.ws))

    def test_a_picker_command_is_declined_and_not_applied(self):
        from unittest import mock
        with mock.patch.object(handler, "apply_picker") as applied:
            rc, _ = self.run_handler("task-staged-2", W, source="worker-picker")
        self.assertEqual(rc, handler.DECLINE)
        applied.assert_not_called()
        self.assertEqual(self.sentinels(W), [])

    def test_without_a_payload_anywhere_nothing_is_delivered(self):
        import pool_router
        self.assertEqual(pool_router.deliver_one(self.ws, W, "task-staged-3",
                                                 payload=self.ws / "tasks" / ".gone.tmp"), "no-payload")
        self.assertEqual(self.sentinels(W), [])


if __name__ == "__main__":
    unittest.main()
