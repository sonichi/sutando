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
- After every publish the lane re-raises the worker's sentinel, so a worker's watcher that gave up on the
  bare pointer (a slow first serve, or a lane that died and the task was served again) is told.

Run: python3 tests/worker-lane-native-pickup.test.py
"""
from _helpers.hermetic_env import isolate_os_environ
from _helpers.worker_identity import give_identities

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
from delivery.task_dispatch import WorkerHoldUnreadable, worker_committed, worker_holds
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
        give_identities(self.ws, W)
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
            after = hook(t, s, d)
            seen["held_before_publish"] = worker_holds(self.ws / "deliveries", d.name)
            seen["published_before_route"] = d.exists()
            seen["after"] = after
            return after
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

    def test_a_fresh_delivery_is_re_raised_once_published(self):
        tid = "task-lane-8"
        sentinel = self.ws / "deliveries" / W / f"{tid}.txt"
        inodes = []
        self.declare_handler()
        self.rgb._install_worker_lane(W)
        hook = self.rgb.TASK_PRE_PUBLISH

        def watching(t, s, d):
            after = hook(t, s, d)
            inodes.append(sentinel.stat().st_ino)
            return after
        self.rgb.TASK_PRE_PUBLISH = watching
        staged, dest = self.stage(tid, W)
        self.assertTrue(self.rgb._publish_task(tid, staged, dest))
        self.assertNotEqual(sentinel.stat().st_ino, inodes[0], "a watcher that gave up in the gap sees it again")
        self.assertEqual(self.sentinels(W), [f"{tid}.txt"])

    def test_a_task_its_worker_already_held_is_re_raised_once_published(self):
        # An earlier serve's lane wrote the sentinel and died before publishing: its watcher gave up.
        tid = "task-lane-9"
        self.declare_handler()
        staged, dest = self.stage(tid, W)
        self.assertEqual(handler.guarded_main(["--task-file", str(dest), "--payload", str(staged), "--workspace",
                                               str(self.ws), "--from-lane"]), 0)
        staged.unlink()
        sentinel = self.ws / "deliveries" / W / f"{tid}.txt"
        before = sentinel.stat().st_ino
        payload, seen = self.publish(tid, W)
        self.assertIsNotNone(seen["after"])
        self.assertNotEqual(sentinel.stat().st_ino, before, "a new file at the sentinel's name: a new event")
        self.assertEqual(self.sentinels(W), [f"{tid}.txt"])
        self.assertEqual(sorted(p.name for p in sentinel.parent.iterdir() if p.name.endswith(".renew")), [])
        self.assertEqual(resolve_inbox_entry.resolve(str(sentinel), self.ws), payload)

    def test_a_router_that_hung_after_committing_is_still_followed_by_a_re_raise(self):
        import subprocess
        from unittest import mock
        route = self.rgb._lane_router(W, ["router"])
        staged, dest = self.stage("task-lane-10", W)
        with mock.patch.object(subprocess, "run", side_effect=[subprocess.TimeoutExpired("router", 10),
                                                               OSError("gone")]) as run:
            after = route("task-lane-10", staged, dest)
            self.assertIsNotNone(after, "a router killed at the bound may have committed")
            after()
        self.assertEqual(run.call_args.args[0], ["router", "--task-file", str(dest), "--renotify"])
        self.assertTrue(any("re-raising task-lane-10 failed" in line for line in self.logged), self.logged)

    def test_a_post_publish_step_that_raises_leaves_the_task_published(self):
        def broken():
            raise RuntimeError("boom")
        self.rgb.TASK_PRE_PUBLISH = lambda *_: broken
        staged, dest = self.stage("task-lane-11", W)
        self.assertTrue(self.rgb._publish_task("task-lane-11", staged, dest))
        self.assertTrue(dest.is_file())
        self.assertTrue(any("post-publish step failed for task-lane-11" in line for line in self.logged),
                        self.logged)

    def test_no_post_publish_step_when_the_publish_fails(self):
        from unittest import mock
        ran = []
        self.rgb.TASK_PRE_PUBLISH = lambda *_: (lambda: ran.append(1))
        staged, dest = self.stage("task-lane-12", W)
        with mock.patch.object(self.rgb, "_publish_staged", return_value=False):
            self.assertFalse(self.rgb._publish_task("task-lane-12", staged, dest))
        self.assertEqual(ran, [])

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

    def test_a_worker_whose_agent_row_is_disabled_gets_nothing_and_the_core_routes_it(self):
        give_identities(self.ws, W, disabled=True)
        payload, seen = self.publish("task-lane-5", W)
        self.assertFalse(seen["held_before_publish"])
        self.assertEqual(self.sentinels(W), [])
        self.assertTrue(payload.is_file())

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

    def test_a_lanes_re_raise_never_applies_a_picker_command(self):
        from unittest import mock
        staged, dest = self.stage("task-staged-5", W, "worker-picker")
        dest.write_text(staged.read_text())
        with mock.patch.object(handler, "apply_picker") as applied:
            rc = handler.guarded_main(["--task-file", str(dest), "--workspace", str(self.ws), "--from-lane",
                                       "--renotify"])
        self.assertEqual(rc, handler.DECLINE)
        applied.assert_not_called()

    def test_an_accepted_sentinel_is_never_re_raised(self):
        import pool_router
        tid = "task-staged-4"
        folder = self.ws / "deliveries" / W
        folder.mkdir()
        (folder / f"{tid}.accepted").touch()
        staged, _ = self.stage(tid, W)
        self.assertEqual(pool_router.deliver_one(self.ws, W, tid, payload=staged, renotify=True), "already")
        self.assertEqual(sorted(p.name for p in folder.iterdir() if p.name.startswith("task-")), [f"{tid}.accepted"])

    def test_without_a_payload_anywhere_nothing_is_delivered(self):
        import pool_router
        self.assertEqual(pool_router.deliver_one(self.ws, W, "task-staged-3",
                                                 payload=self.ws / "tasks" / ".gone.tmp"), "no-payload")
        self.assertEqual(self.sentinels(W), [])


class ASentinelLongBeforeItsPayload(Base):
    """The worker's watcher gives up on a sentinel whose payload is not there, and its catch-up pass never
    revisits an entry it has passed. The lane re-raises the sentinel after every publish, so the worker is
    told: whether the gap was a slow first serve or a lane that died and the task was served again."""

    def start_worker_watcher(self):
        import os
        import subprocess
        sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
        sys.path.insert(0, str(ROOT / "tests"))
        from clean_watcher_env import clean_env
        from _helpers.process_group import popen_bound
        inbox = self.ws / "deliveries" / W
        inbox.mkdir()
        feed = self.ws / "feed"
        feed.write_text("")
        b = self.ws / "bin"
        b.mkdir()
        (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
        (b / "fswatch").chmod(0o755)
        env = clean_env()
        env.update(PATH=f"{b}:{env['PATH']}", TMPDIR=str(self.ws), SUTANDO_WORKSPACE_DIR=str(self.ws),
                   SUTANDO_RESULTS_DIR=str(self.ws / "results"), SUTANDO_INSTANCE_ID=W,
                   SUTANDO_INBOX_RESOLVER=str(ROOT / "skills" / "worker-pool" / "scripts" / "resolve_inbox_entry.py"),
                   SUTANDO_WATCHER_CATCHUP_SECONDS="1")
        errf = open(self.ws / "watcher.err", "w+")
        self.addCleanup(errf.close)
        p = popen_bound(["bash", "src/watch-tasks-stream.sh", str(inbox), "--role", "standby", "--inbox", str(inbox)],
                        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=errf)
        os.set_blocking(p.stdout.fileno(), False)
        out = [b""]

        def wait_for(cond, deadline=30.0):
            end = time.time() + deadline
            while time.time() < end and p.poll() is None:
                try:
                    out[0] += os.read(p.stdout.fileno(), 65536) or b""
                except BlockingIOError:
                    pass
                errf.seek(0)
                if cond(out[0].decode(), errf.read()):
                    return True
                time.sleep(0.1)
            return False

        def stop():
            try:
                os.killpg(os.getpgid(p.pid), 15)
            except OSError:
                pass
            p.wait(timeout=10)
            p.stdout.close()
        self.addCleanup(stop)
        self.declare_handler()
        self.rgb._install_worker_lane(W)
        return inbox, feed, out, wait_for

    def gave_up(self, tid, inbox, feed, out, wait_for):
        with feed.open("a") as f:
            f.write(f"{inbox / (tid + '.txt')}\n")
        self.assertTrue(wait_for(lambda o, e: "did not resolve" in e and f"{tid}.txt" in e), "the watcher gave up")
        time.sleep(2.5)   # past a catch-up pass, which skips the entry as already passed
        self.assertNotIn(tid, out[0].decode())

    def test_a_slow_first_serve(self):
        tid = "task-slow-1"
        inbox, feed, out, wait_for = self.start_worker_watcher()
        hook = self.rgb.TASK_PRE_PUBLISH

        def slow(t, s, d):
            after = hook(t, s, d)
            self.gave_up(tid, inbox, feed, out, wait_for)
            return after
        self.rgb.TASK_PRE_PUBLISH = slow
        staged, dest = self.stage(tid, W)
        self.assertTrue(self.rgb._publish_task(tid, staged, dest))
        self.assertTrue(wait_for(lambda o, e: f"/tasks/{tid}.txt" in o and "TASK_FILE:" in o),
                        "the worker is told of its task")

    def test_a_lane_that_died_before_publishing(self):
        tid = "task-died-1"
        inbox, feed, out, wait_for = self.start_worker_watcher()
        staged, dest = self.stage(tid, W)
        self.assertIsNotNone(self.rgb.TASK_PRE_PUBLISH(tid, staged, dest))   # routed; the lane dies here
        staged.unlink()
        self.gave_up(tid, inbox, feed, out, wait_for)
        staged, dest = self.stage(tid, W)
        self.assertTrue(self.rgb._publish_task(tid, staged, dest))
        self.assertTrue(wait_for(lambda o, e: f"/tasks/{tid}.txt" in o and "TASK_FILE:" in o),
                        "the worker is told of its task")


class AnInterruptedLane(Base):
    """The lane's router died after the sentinel and before the attribution: not committed, so the core's
    watcher does not skip it, and its router pass repairs the attribution the bridge reads for the reply."""

    def test_the_core_finishes_a_delivery_the_lane_left_half_made(self):
        import pool_router
        self.declare_handler()
        tid = "task-half-1"
        staged, dest = self.stage(tid, W)
        dest.write_text(staged.read_text())
        staged.unlink()
        with self.assertRaises(RuntimeError):
            def die():
                raise RuntimeError("router killed")
            pool_router.deliver_one(self.ws, W, tid, _between_sentinel_and_attribution=die)
        self.assertTrue(worker_holds(self.ws / "deliveries", dest.name))
        self.assertFalse(worker_committed(self.ws, dest.name), "a sentinel alone is not a committed delivery")
        rc = handler.guarded_main(["--task-file", str(dest), "--workspace", str(self.ws),
                                   "--results-dir", str(self.ws / "results")])
        self.assertEqual(rc, 0)
        self.assertTrue(worker_committed(self.ws, dest.name))
        self.assertEqual(self.sentinels(W), [f"{tid}.txt"])
        saved = self.rgb._STATE
        self.addCleanup(setattr, self.rgb, "_STATE", saved)
        self.rgb._STATE = self.ws / "state"
        self.assertEqual(self.rgb._assigned_worker(tid), W, "the bridge attributes the worker's reply")

    def test_committed_means_the_attribution_names_a_holder(self):
        tid = "task-half-2"
        folder = self.ws / "deliveries" / W
        folder.mkdir()
        (folder / f"{tid}.accepted").touch()
        att = pa.attribution_dir(self.ws)
        att.mkdir(parents=True)
        for value, want in ((OTHER, False), ("", False), ("..", False), (W, True)):
            with self.subTest(value=value):
                (att / tid).write_text(value)
                self.assertEqual(worker_committed(self.ws, f"{tid}.txt"), want)
        self.assertFalse(worker_committed(self.ws, "task-nobody.txt"))

    def test_an_unreadable_record_cannot_decide(self):
        import os
        from unittest import mock
        tid = "task-half-3"
        folder = self.ws / "deliveries" / W
        folder.mkdir()
        (folder / f"{tid}.txt").touch()
        pa.attribution_dir(self.ws).mkdir(parents=True)
        (pa.attribution_dir(self.ws) / tid).mkdir()
        with self.assertRaises(WorkerHoldUnreadable):
            worker_committed(self.ws, f"{tid}.txt")
        (pa.attribution_dir(self.ws) / tid).rmdir()
        (pa.attribution_dir(self.ws) / tid).write_text(W)
        real = os.lstat

        def flaky(path, *a, **k):
            if str(path).endswith(f"{W}/{tid}.txt") and flaky.armed:
                raise PermissionError("denied")
            return real(path, *a, **k)
        flaky.armed = False
        with mock.patch("os.lstat", side_effect=flaky):
            import delivery.task_dispatch as td
            with mock.patch.object(td, "worker_holds", return_value=True):
                flaky.armed = True
                with self.assertRaises(WorkerHoldUnreadable):
                    worker_committed(self.ws, f"{tid}.txt")

    def test_the_cli(self):
        import subprocess
        tid = "task-half-4"
        script = str(ROOT / "src" / "delivery" / "task_dispatch.py")

        def cli(*args):
            import contextlib
            import io
            import delivery.task_dispatch as td
            with contextlib.redirect_stderr(io.StringIO()):
                rc = td._main(["worker-committed", *args])
            self.assertEqual(subprocess.run([sys.executable, script, "worker-committed", *args],
                                            capture_output=True).returncode, rc)
            return rc
        self.assertEqual(cli(str(self.ws), f"{tid}.txt"), 1)
        folder = self.ws / "deliveries" / W
        folder.mkdir()
        (folder / f"{tid}.txt").touch()
        pa.attribution_dir(self.ws).mkdir(parents=True)
        (pa.attribution_dir(self.ws) / tid).mkdir()
        self.assertEqual(cli(str(self.ws), f"{tid}.txt"), 2)
        (pa.attribution_dir(self.ws) / tid).rmdir()
        (pa.attribution_dir(self.ws) / tid).write_text(W)
        self.assertEqual(cli(str(self.ws), f"{tid}.txt"), 0)
        self.assertEqual(cli(str(self.ws), f"{tid}.txt", "extra"), 2)


if __name__ == "__main__":
    unittest.main()
