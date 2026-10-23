#!/usr/bin/env python3
"""sparrowd follows the agent-bridge declarations as they come and go, in both of its modes.

The package shell syncs its supervised set by name (a new name starts, a dropped name stops,
an unchanged name is left running); the launcher feeds it the declarations on every scan, prints
each skip reason once, and never lists a declaration another process already runs. `--agent-bridges`
is the mode the launchd wrapper keeps alive beside the gateway bridge: the declarations only.
Run: python3 tests/sparrowd-agent-bridge-rescan.test.py"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO / "src"), str(REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sparrow_agent_bridge as bridge  # noqa: E402
from ag2_sparrow.sparrowd import Supervisor, WorkerSpec, run  # noqa: E402

from ag2_sparrow.adapters.claude_code import cli  # noqa: E402

SLEEPER = [sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"]
STUBBORN = [sys.executable, "-c", "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                  "while True: time.sleep(0.2)"]


def wait_for(pred, timeout=8.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


def alive(pid) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pid_of(sup, name):
    return sup.status()["workers"][name]["pid"]


class Reconcile(unittest.TestCase):
    def test_a_new_name_starts_a_dropped_name_stops_and_the_rest_keep_their_pid(self):
        sup = Supervisor([WorkerSpec("a", SLEEPER)])
        sup.start()
        self.addCleanup(sup.stop, 2)
        self.assertTrue(wait_for(lambda: pid_of(sup, "a")))
        a_pid = pid_of(sup, "a")
        self.assertEqual(sup.sync([WorkerSpec("a", SLEEPER), WorkerSpec("b", SLEEPER)]), (["b"], []))
        self.assertTrue(wait_for(lambda: pid_of(sup, "b")), "the added name is started")
        b_pid = pid_of(sup, "b")
        self.assertEqual(sup.sync([WorkerSpec("a", SLEEPER), WorkerSpec("b", SLEEPER)]), ([], []))
        self.assertEqual((pid_of(sup, "a"), pid_of(sup, "b")), (a_pid, b_pid), "an unchanged set restarts nothing")
        self.assertEqual(sup.sync([WorkerSpec("a", SLEEPER)]), ([], ["b"]))
        self.assertTrue(wait_for(lambda: not alive(b_pid)), "the dropped worker's process is gone")
        self.assertNotIn("b", sup.status()["workers"])
        self.assertEqual(pid_of(sup, "a"), a_pid, "the kept worker was not touched")
        self.assertEqual(sup.sync([]), ([], ["a"]))
        self.assertTrue(wait_for(lambda: not alive(a_pid)))
        self.assertEqual(sup.status()["workers"], {})

    def test_a_changed_spec_restarts_its_worker_and_never_runs_two_for_the_name(self):
        sup = Supervisor([WorkerSpec("s", STUBBORN, revision="1")], remove_grace_s=0.6)
        sup.start()
        self.addCleanup(sup.stop, 2)
        self.assertTrue(wait_for(lambda: pid_of(sup, "s")))
        old = pid_of(sup, "s")
        time.sleep(0.3)  # the handler is installed before the loop
        self.assertEqual(sup.sync([WorkerSpec("s", STUBBORN, revision="1")]), ([], []), "same spec: untouched")
        self.assertEqual(pid_of(sup, "s"), old)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sup.sync([WorkerSpec("s", SLEEPER, revision="2")]), (["s"], ["s"]))
            time.sleep(0.3)
            self.assertTrue(alive(old), "the old one ignores SIGTERM and is inside its grace")
            self.assertIsNone(pid_of(sup, "s"), "its replacement waits: never two processes for one name")
            self.assertEqual(sup.status()["draining"], ["s"])
            self.assertTrue(wait_for(lambda: pid_of(sup, "s"), 5))
        self.assertFalse(alive(old), "killed past the grace before the replacement started")
        self.assertIn("worker s: changed — restarting", out.getvalue())
        self.assertNotIn("worker s: added", out.getvalue())

    def test_a_replacement_stopped_while_it_waits_never_spawns(self):
        sup = Supervisor([WorkerSpec("w", SLEEPER)])
        ws = sup._workers["w"]
        ws.after = [types.SimpleNamespace(proc=types.SimpleNamespace(poll=lambda: None))]  # never exits

        def stop():  # a loop that spawned anyway is ended too, so a regression fails instead of hanging
            ws.stop.set()
            if ws.proc is not None:
                ws.proc.kill()
        threading.Timer(0.2, stop).start()
        with contextlib.redirect_stdout(io.StringIO()):
            sup._run_worker(ws)
        self.assertIsNone(ws.proc)
        self.assertEqual(ws.state, "stopped")

    def test_a_removed_worker_that_ignores_sigterm_is_killed_past_the_grace(self):
        sup = Supervisor([WorkerSpec("s", STUBBORN)], remove_grace_s=0.3)
        sup.start()
        self.addCleanup(sup.stop, 2)
        self.assertTrue(wait_for(lambda: pid_of(sup, "s")))
        pid = pid_of(sup, "s")
        time.sleep(0.3)  # the handler is installed before the loop; give it that
        sup.sync([])
        self.assertTrue(wait_for(lambda: not alive(pid), 5), "SIGKILL after the grace, no orphan")

    def test_a_removed_worker_in_backoff_never_respawns(self):
        crasher = [sys.executable, "-c", "raise SystemExit(3)"]
        sleeps = []
        sup = Supervisor([WorkerSpec("c", crasher)], backoff_initial_s=0.5,
                         sleep=lambda s: (sleeps.append(s), time.sleep(0.05)))
        sup.start()
        self.addCleanup(sup.stop, 2)
        self.assertTrue(wait_for(lambda: len(sleeps) >= 1))
        sup.sync([])
        restarts = sup.status()["workers"]
        self.assertEqual(restarts, {}, "gone from the table at once")
        n = len(sleeps)
        time.sleep(0.6)
        self.assertLessEqual(len(sleeps), n + 1, "at most the pause in flight; no further restart")

    # The worker loop normally runs on a supervisor thread, which the gate's
    # tracer does not follow; these drive it on the calling thread instead.
    def test_the_worker_loop_stops_after_the_pause_that_sees_the_stop(self):
        crasher = [sys.executable, "-c", "raise SystemExit(3)"]
        sup = Supervisor([WorkerSpec("c", crasher)], backoff_initial_s=0.2,
                         sleep=lambda s: ws.stop.set())
        ws = sup._workers["c"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sup._run_worker(ws)
        self.assertEqual((ws.last_exit, ws.restarts, ws.state), (3, 1, "stopped"))
        self.assertIn("worker c: exited rc=3 — restart in 0.2s (restart #1)", out.getvalue())
        self.assertNotIn("restart #2", out.getvalue(), "the pause saw the stop: no respawn")

    def test_a_stop_that_lands_while_the_loop_waits_for_the_lock_prevents_the_spawn(self):
        sup = Supervisor([WorkerSpec("n", SLEEPER)])
        ws = sup._workers["n"]
        sup._lock.acquire()  # the loop passes its while check, then blocks here

        def stop_then_release():
            time.sleep(0.1)
            ws.stop.set()
            sup._lock.release()
        threading.Thread(target=stop_then_release).start()
        with contextlib.redirect_stdout(io.StringIO()):
            sup._run_worker(ws)
        self.assertIsNone(ws.proc, "never spawned: the stop was seen under the lock")
        self.assertEqual(ws.state, "stopped")

    def test_a_spawn_failure_pauses_and_a_pause_without_injected_sleep_waits_on_the_stop(self):
        sup = Supervisor([WorkerSpec("g", ["/nonexistent/bin/worker"])], backoff_initial_s=0.05)
        ws = sup._workers["g"]
        threading.Timer(0.2, ws.stop.set).start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sup._run_worker(ws)
        self.assertIn("worker g: spawn failed", out.getvalue())
        self.assertEqual((ws.last_exit, ws.state), (None, "stopped"))

    def test_a_worker_stopped_while_running_ends_the_loop_as_stopped(self):
        sup = Supervisor([WorkerSpec("r", SLEEPER)])
        ws = sup._workers["r"]

        def stop_it():
            wait_for(lambda: ws.proc is not None)
            ws.stop.set()
            ws.proc.terminate()
        threading.Timer(0.05, stop_it).start()
        with contextlib.redirect_stdout(io.StringIO()):
            sup._run_worker(ws)
        self.assertEqual((ws.state, ws.restarts), ("stopped", 0), "a stop during the run is not a crash")
        self.assertIsNotNone(ws.last_exit)

    def test_wait_kills_a_removed_worker_past_the_grace(self):
        sup = Supervisor([WorkerSpec("s", STUBBORN)], remove_grace_s=0.2)
        ws = sup._workers["s"]
        with contextlib.redirect_stdout(io.StringIO()):
            sup._spawn(ws)
        time.sleep(0.3)  # the handler is installed before the loop
        ws.stop.set()
        ws.stop_at = time.monotonic()
        ws.proc.send_signal(15)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sup._wait(ws)
        self.assertEqual(rc, -9)
        self.assertIn(f"pid {ws.proc.pid}: did not exit in 0.2s — SIGKILL", out.getvalue())

    def test_run_syncs_on_every_rescan(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        passes = [[], [WorkerSpec("late", SLEEPER)], [WorkerSpec("late", SLEEPER)], []]
        seen = []
        sock = td / "sparrowd.sock"

        def rescan():
            want = passes.pop(0) if passes else []
            seen.append([s.name for s in want])
            return want

        def stop_when_done():
            wait_for(lambda: sock.exists() and len(seen) >= 4, 10)
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock))
            c.sendall(b'{"op": "stop"}\n')
            c.makefile("r").readline()
            c.close()
        th = threading.Thread(target=stop_when_done, daemon=True)
        th.start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run([], td, rescan=rescan, rescan_s=0.1)
        th.join(5)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(seen), 4)
        log = out.getvalue()
        self.assertEqual(log.count("worker late: added"), 1, "one worker for the declaration while it existed")
        self.assertEqual(log.count("worker late: removed"), 1, "stopped once it was gone")

    def test_an_unavailable_rescan_keeps_the_running_workers_and_is_logged_once_per_episode(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        sock = td / "sparrowd.sock"
        calls = []
        # two failing passes, one good, two failing, one good: two episodes
        script = ["fail", "fail", "ok", "fail", "fail", "ok"]

        def rescan():
            calls.append(1)
            if script and script.pop(0) == "fail":
                raise RuntimeError("vault down")
            return [WorkerSpec("w", SLEEPER)]

        def stop_when_done():
            wait_for(lambda: sock.exists() and len(calls) >= 7, 10)
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock))
            c.sendall(b'{"op": "stop"}\n')
            c.makefile("r").readline()
            c.close()
        threading.Thread(target=stop_when_done, daemon=True).start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run([WorkerSpec("w", SLEEPER)], td, rescan=rescan, rescan_s=0.1)
        self.assertEqual(rc, 0)
        log = out.getvalue()
        self.assertEqual(log.count("rescan unavailable (RuntimeError: vault down)"), 2, "once per episode")
        self.assertEqual(log.count("rescan recovered"), 2)
        self.assertNotIn("worker w: removed", log)
        self.assertEqual(log.count("worker w: started"), 1, "never restarted across the episodes")

    def test_a_removed_worker_that_ignores_sigterm_is_reaped_before_shutdown_is_reported(self):
        """The reviewed scenario: removed on one rescan, sparrowd signalled on the next; the retiring
        worker must not outlive "all workers stopped". The control run keeps it supervised."""
        for removed in (True, False):
            td = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, td, ignore_errors=True)
            hb = td / "heartbeat"
            worker = WorkerSpec("stubborn", [sys.executable, "-c", (
                "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"while True:\n open({str(hb)!r}, 'a').write('.')\n time.sleep(0.05)")])
            passes = [[] if removed else [worker]]
            seen = []

            def rescan():
                want = passes.pop(0) if passes else ([] if removed else [worker])
                seen.append(want)
                if len(seen) == 2:
                    os.kill(os.getpid(), signal.SIGTERM)
                return want
            wait_for(hb.exists, 5)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = run([worker], td, rescan=rescan, rescan_s=0.3, grace_s=0.5)
            log = out.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("all workers stopped", log)
            self.assertEqual("worker stubborn: removed — stopping" in log, removed)
            pid = int(log.split("worker stubborn: started pid ")[1].split()[0])
            self.assertFalse(alive(pid), f"removed={removed}: the worker outlived shutdown")
            size = hb.stat().st_size
            time.sleep(0.3)
            self.assertEqual(hb.stat().st_size, size, f"removed={removed}: still writing after shutdown")
            self.assertIn("SIGKILL", log)

    def test_two_quick_edits_never_run_two_processes_for_one_agent_under_run(self):
        """The reviewed case: A ignores SIGTERM, an edit makes B, a second edit makes C before A has
        exited. B never starts, C starts only once A is gone, and no sample sees two live processes."""
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        live = td / "live"
        live.mkdir()
        code = ("import os, signal, sys, time\nlive, label = sys.argv[1], sys.argv[2]\n"
                "if label == 'A':\n signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "a = os.path.join(live, 'A')\nwas = 'n/a'\n"
                "if label != 'A' and os.path.exists(a):\n"
                " try:\n  os.kill(int(open(a).read()), 0); was = 'alive'\n except OSError:\n  was = 'dead'\n"
                "open(os.path.join(live, label + '.A'), 'w').write(was)\n"
                "open(os.path.join(live, label), 'w').write(str(os.getpid()))\n"
                "while True: time.sleep(0.05)")

        def spec(label):
            return WorkerSpec("agent", [sys.executable, "-c", code, str(live), label], revision=label)
        passes = [[spec("B")], [spec("C")]]
        calls, samples, peak = [], [], [0]
        sock = td / "sparrowd.sock"

        def rescan():
            calls.append(1)
            return passes.pop(0) if passes else [spec("C")]

        def pids():
            out = []
            for f in live.iterdir():
                if f.suffix != ".A" and f.read_text():
                    out.append(int(f.read_text()))
            return out

        def sample_then_stop():
            wait_for(lambda: (live / "A").exists(), 5)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not ((live / "C").exists() and len(calls) >= 6):
                n = sum(alive(p) for p in pids())
                samples.append(n)
                peak[0] = max(peak[0], n)
                time.sleep(0.01)
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock))
            c.sendall(b'{"op": "stop"}\n')
            c.makefile("r").readline()
            c.close()
        th = threading.Thread(target=sample_then_stop, daemon=True)
        th.start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run([spec("A")], td, rescan=rescan, rescan_s=0.15, grace_s=1.2)
        th.join(5)
        self.assertEqual(rc, 0)
        self.assertGreater(len(samples), 20)
        self.assertLessEqual(peak[0], 1, "never two live processes for one agent at any sample")
        self.assertFalse((live / "B").exists(), "the superseded replacement never started: dropped")
        self.assertEqual((live / "C.A").read_text(), "dead", "C started only after A had exited")

    def test_a_held_lock_is_waited_for_with_one_line_not_exited(self):
        """Relaunched by the wrapper, an instance that exited on a held lock logged a line every
        RESTART_DELAY; waiting, it logs once when the wait starts and once when it ends."""
        from ag2_sparrow.sparrowd import SingleInstance
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        holder = SingleInstance(td / "sparrowd.lock")
        self.assertTrue(holder.acquire())
        sock = td / "sparrowd.sock"

        def release_then_stop():
            time.sleep(0.5)
            holder.release()
            wait_for(sock.exists, 5)
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock))
            c.sendall(b'{"op": "stop"}\n')
            c.makefile("r").readline()
            c.close()
        threading.Thread(target=release_then_stop, daemon=True).start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run([], td, wait_for_lock=True, lock_poll_s=0.05)
        log = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertEqual(log.count("another sparrowd holds the lock — waiting for it"), 1)
        self.assertEqual(log.count("lock acquired"), 1)
        self.assertNotIn("exiting cleanly", log)
        self.assertIn("starting 0 worker(s)", log)

    def test_a_stop_while_waiting_for_the_lock_exits_cleanly(self):
        from ag2_sparrow.sparrowd import SingleInstance
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        holder = SingleInstance(td / "sparrowd.lock")
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM)).start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run([], td, wait_for_lock=True, lock_poll_s=0.05), 0)
        self.assertIn("stopped while waiting for the lock", out.getvalue())
        self.assertNotIn("starting", out.getvalue())

    def test_a_removed_worker_stays_owned_until_it_has_exited(self):
        sup = Supervisor([WorkerSpec("s", STUBBORN)], remove_grace_s=0.3)
        sup.start()
        self.addCleanup(sup.stop, 2)
        self.assertTrue(wait_for(lambda: pid_of(sup, "s")))
        pid = pid_of(sup, "s")
        time.sleep(0.3)
        sup.sync([])
        self.assertEqual(sup.status()["draining"], ["s"], "removed, not yet exited: still listed as owned")
        self.assertTrue(wait_for(lambda: not alive(pid), 5))
        sup.sync([])
        self.assertEqual(sup.status()["draining"], [], "exited: released")


AGENT = "@probe.agent:ag2.space"
SEG = "probe.agent_ag2.space"


def specs_of(case, decl):
    """The launcher's spec for one declaration, as the scan builds it."""
    specs, _ = case.sd._agent_bridge_specs(case.ws, vault_keys=list(case.keys))
    return next(s for s in specs if s.argv[-1] == str(decl))


def load_sparrowd():
    spec = importlib.util.spec_from_file_location("sparrowd_rescan_under_test", REPO / "src" / "sparrowd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VaultScan(unittest.TestCase):
    """The reviewed scenario: the production vault reader on an isolated manifest that becomes a
    directory (a real IsADirectoryError) through the real scanner into run(): the healthy worker is
    never stopped, and the count goes 1 -> unavailable -> 1, not 1 -> 0 -> 1."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.sd = load_sparrowd()
        sys.modules.pop("vault_intercept", None)
        for patch in (mock.patch.object(self.sd, "resolve_workspace", return_value=self.ws),
                      mock.patch("workspace_default.resolve_workspace", return_value=self.ws),
                      mock.patch.object(self.sd, "external_supervisor", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)
        root = self.ws / "agents" / SEG
        cfg = {"ROOT": str(root), "BRIDGE": "sparrowd", "AGENT_ID": AGENT, "TOKEN_VAULT_KEY": cli.vault_key(SEG)}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.wire_bridge(cfg), "")
        self.manifest = self.ws / "state" / "secret-vault" / "keys.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(json.dumps({cli.vault_key(SEG): {"added": "fixture"}}))

    def break_manifest(self):
        self.manifest.unlink()
        self.manifest.mkdir()

    def mend_manifest(self):
        self.manifest.rmdir()
        self.manifest.write_text(json.dumps({cli.vault_key(SEG): {"added": "fixture"}}))

    def test_a_vault_read_error_is_unavailable_not_empty(self):
        import vault_intercept
        scan = self.sd.AgentBridgeScan(self.ws)
        self.assertEqual([s.name for s in scan()], [cli.worker_name(SEG)])
        self.break_manifest()
        with self.assertRaises(IsADirectoryError):
            vault_intercept.list_vault_keys()
        with self.assertRaises(bridge.ScanUnavailable) as cm:
            scan()
        self.assertIn("vault manifest unreadable (IsADirectoryError", str(cm.exception))
        self.mend_manifest()
        self.assertEqual([s.name for s in scan()], [cli.worker_name(SEG)])

    def test_the_running_bridge_survives_the_vault_error_under_run(self):
        td = self.ws / "state" / "sparrowd"
        scan = self.sd.AgentBridgeScan(self.ws)
        counts, passes = [], []

        def rescan():
            # the production scan decides the names; the fixture stands in for the bridge process
            passes.append(1)
            try:
                want = [WorkerSpec(s.name, SLEEPER) for s in scan()]
            except bridge.ScanUnavailable:
                if len(passes) >= 3:  # two unavailable passes, then the manifest is back
                    self.mend_manifest()
                raise
            counts.append(len(want))
            if len(counts) == 1:
                self.break_manifest()
            return want

        def stop_when_done():
            wait_for(lambda: (td / "sparrowd.sock").exists() and len(counts) >= 2, 10)
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(td / "sparrowd.sock"))
            c.sendall(b'{"op": "stop"}\n')
            c.makefile("r").readline()
            c.close()
        threading.Thread(target=stop_when_done, daemon=True).start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run([WorkerSpec(cli.worker_name(SEG), SLEEPER)], td, rescan=rescan, rescan_s=0.1)
        self.assertEqual(rc, 0)
        log = out.getvalue()
        self.assertEqual(counts[:2], [1, 1], "the count never reads 0: an unavailable scan is not an empty one")
        self.assertGreaterEqual(len(passes), 4)
        self.assertEqual(log.count("rescan unavailable (ScanUnavailable: vault manifest unreadable (IsADirectoryError"), 1)
        self.assertIn("rescan recovered", log)
        self.assertNotIn("removed", log)
        self.assertEqual(log.count("started pid"), 1, "the healthy worker was never terminated")


class Launcher(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.sd = load_sparrowd()
        self.keys = []
        fake_vault = types.SimpleNamespace(list_vault_keys=lambda: list(self.keys))
        for patch in (mock.patch.object(self.sd, "resolve_workspace", return_value=self.ws),
                      mock.patch.dict(sys.modules, {"vault_intercept": fake_vault})):
            patch.start()
            self.addCleanup(patch.stop)

    def declare(self, agent=AGENT, seg=SEG):
        root = self.ws / "agents" / seg
        cfg = {"ROOT": str(root), "BRIDGE": "sparrowd", "AGENT_ID": agent, "TOKEN_VAULT_KEY": cli.vault_key(seg)}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.wire_bridge(cfg), "")
        self.keys.append(cli.vault_key(seg))
        return cli.bridge_home(root) / cli.DECLARATION

    def main(self, argv, external=None):
        """main with `run` captured; the external-supervisor stand-in stays in place for the rescans."""
        err = io.StringIO()
        ext = mock.patch.object(self.sd, "external_supervisor", side_effect=external or (lambda m: None))
        ext.start()
        self.addCleanup(ext.stop)
        with mock.patch.object(self.sd, "run", return_value=0) as run_, contextlib.redirect_stderr(err):
            rc = self.sd.main(argv)
        self.assertEqual(rc, 0)
        args, kw = run_.call_args
        return args[0], args[1], kw["rescan"], kw["rescan_s"], err

    def test_agent_bridges_mode_supervises_only_the_declarations_and_follows_them(self):
        specs, state, rescan, rescan_s, err = self.main(["--agent-bridges", "--rescan-s", "0.5"])
        self.assertEqual((specs, state, rescan_s), ([], self.ws / "state" / "sparrowd", 0.5),
                         "nothing to run yet; the gateway bridge and the skill loops are not this mode's")
        decl = self.declare()
        with contextlib.redirect_stderr(err):
            want = rescan()
        self.assertEqual([s.name for s in want], [cli.worker_name(SEG)], "written after start: picked up on a pass")
        self.assertEqual(want[0].argv[1:], [str(self.sd.AGENT_BRIDGE), str(decl)], "the one wrapper runs it")
        decl.unlink()
        with contextlib.redirect_stderr(err):
            self.assertEqual(rescan(), [], "removed by uninstall: gone on the next pass")
        self.assertEqual(err.getvalue(), "")

    def test_full_mode_keeps_the_gateway_bridge_first_and_rescans_only_the_declarations(self):
        self.declare()
        specs, _, rescan, rescan_s, _ = self.main([])
        self.assertEqual(rescan_s, 30.0)
        names = [s.name for s in specs]
        self.assertEqual((names[0], names.count(cli.worker_name(SEG))), ("remote-gateway-bridge", 1))
        with contextlib.redirect_stderr(io.StringIO()):
            again = [s.name for s in rescan()]
        self.assertEqual(again, names, "the static workers are listed on every pass, so never dropped")

    def test_a_skip_reason_is_printed_once_until_it_changes(self):
        decl = self.declare()
        self.keys.clear()
        _, _, rescan, _, err = self.main(["--agent-bridges"])
        why = f"the vault has no {cli.vault_key(SEG)}"
        self.assertEqual(err.getvalue().count(why), 1)
        with contextlib.redirect_stderr(err):
            rescan()
            rescan()
        self.assertEqual(err.getvalue().count(why), 1, "the same reason is not repeated every pass")
        decl.write_text("{torn")
        with contextlib.redirect_stderr(err):
            rescan()
        self.assertEqual(err.getvalue().count("malformed (JSONDecodeError)"), 1, "a new reason is printed")
        decl.unlink()
        with contextlib.redirect_stderr(err):
            self.assertEqual(rescan(), [])

    def test_a_declaration_another_process_runs_is_skipped_and_an_own_worker_is_not_rechecked(self):
        decl = self.declare()
        checked = []

        def external(marker):
            checked.append(marker)
            return "pid 4242 (1 python3 sparrow_agent_bridge.py …)" if len(checked) == 1 else None
        specs, _, rescan, _, err = self.main(["--agent-bridges"], external=external)
        self.assertEqual(specs, [], "live outside this daemon: never a second bridge for the agent")
        self.assertIn(f"{decl}: already supervised outside sparrowd: pid 4242", err.getvalue())
        self.assertEqual(checked, [self.sd.supervision_marker(specs_of(self, decl))])
        with contextlib.redirect_stderr(err):
            want = rescan()
        self.assertEqual([s.name for s in want], [cli.worker_name(SEG)], "picked up once that process is gone")
        self.assertEqual(len(checked), 2)
        with contextlib.redirect_stderr(err):
            rescan()
        self.assertEqual(len(checked), 2, "its live process is now this daemon's own worker: not re-checked")

    def test_a_scan_unavailable_at_start_runs_nothing_yet_and_is_picked_up_on_the_first_good_pass(self):
        decl = self.declare()
        broken = [True]

        def keys():
            if broken[0]:
                raise OSError("manifest locked")
            return [cli.vault_key(SEG)]
        with mock.patch.dict(sys.modules, {"vault_intercept": types.SimpleNamespace(list_vault_keys=keys)}):
            specs, _, rescan, _, err = self.main(["--agent-bridges"])
            self.assertEqual(specs, [])
            self.assertIn("agent bridges not scanned yet — vault manifest unreadable (OSError: manifest locked)",
                          err.getvalue())
            with self.assertRaises(bridge.ScanUnavailable):
                rescan()
            broken[0] = False
            self.assertEqual([s.argv[-1] for s in rescan()], [str(decl)])

    def test_an_undecodable_declaration_is_skipped_with_a_reason_and_the_daemon_starts_the_valid_one(self):
        """The reviewed regression: bytes that are not UTF-8 must never escape main as an exception
        (at the reviewed head none of the valid bridges started); fixed later, it is picked up."""
        good = self.declare()
        bad = self.declare("@second:ag2.space", "second_ag2.space")
        specs = self.main(["--agent-bridges"])[0]
        self.assertEqual([s.argv[-1] for s in specs], [str(good), str(bad)], "both reach the daemon")
        valid = bad.read_text()
        bad.write_bytes(b"\xff\xfe\x00")
        escaped = None
        try:
            specs, _, rescan, _, err = self.main(["--agent-bridges"])
        except Exception as e:  # noqa: BLE001 - the regression is an exception escaping main
            escaped = type(e).__name__
        self.assertIsNone(escaped, "nothing escapes main; the daemon is reached")
        self.assertEqual([s.argv[-1] for s in specs], [str(good)], "the daemon starts with the valid one")
        self.assertEqual(err.getvalue().count(f"not supervised — {bad}: malformed (UnicodeDecodeError)"), 1)
        with contextlib.redirect_stderr(err):
            self.assertEqual([s.argv[-1] for s in rescan()], [str(good)])
        self.assertEqual(err.getvalue().count("UnicodeDecodeError"), 1, "the reason is not repeated")
        bad.write_text(valid)
        with contextlib.redirect_stderr(err):
            self.assertEqual([s.argv[-1] for s in rescan()], [str(good), str(bad)], "fixed: picked up next pass")

    def test_an_unreadable_declaration_keeps_its_worker_and_the_rest_still_follow(self):
        """One declaration that cannot be read must not freeze the others: the reviewed case uninstalls
        B while A is unreadable, and B must stop on that pass while A keeps running unchanged."""
        if os.geteuid() == 0:
            self.skipTest("root reads any file")
        a = self.declare()
        b = self.declare("@second:ag2.space", "second_ag2.space")
        specs, _, rescan, _, err = self.main(["--agent-bridges"])
        self.assertEqual([s.argv[-1] for s in specs], [str(a), str(b)])
        os.chmod(a, 0)
        self.addCleanup(os.chmod, a, 0o644)
        shutil.rmtree(b.parent)
        outcome = None
        with contextlib.redirect_stderr(err):
            try:
                outcome = rescan()
            except bridge.ScanUnavailable as e:
                outcome = f"unavailable: {e}"
            again = rescan() if isinstance(outcome, list) else None
        self.assertIsInstance(outcome, list, "one unreadable declaration is not an unavailable scan")
        self.assertEqual(outcome, [specs[0]], "A kept exactly as it ran (no restart); B gone on this pass")
        self.assertEqual(again, [specs[0]])
        self.assertEqual(err.getvalue().count(f"not supervised — {a}: unreadable (PermissionError)"), 1)
        os.chmod(a, 0o644)
        with contextlib.redirect_stderr(err):
            self.assertEqual(rescan(), [specs[0]], "readable again and unchanged: the same spec, no restart")

    def test_an_unreadable_declaration_at_start_is_not_started(self):
        if os.geteuid() == 0:
            self.skipTest("root reads any file")
        a = self.declare()
        os.chmod(a, 0)
        self.addCleanup(os.chmod, a, 0o644)
        specs, _, rescan, _, err = self.main(["--agent-bridges"])
        self.assertEqual(specs, [], "nothing ran under its name: nothing to keep")
        self.assertIn(f"{a}: unreadable (PermissionError)", err.getvalue())
        os.chmod(a, 0o644)
        with contextlib.redirect_stderr(err):
            self.assertEqual([s.argv[-1] for s in rescan()], [str(a)], "readable: picked up")

    def test_an_edited_declaration_is_a_changed_spec_an_unchanged_one_is_equal(self):
        a = self.declare()
        specs, _, rescan, _, err = self.main(["--agent-bridges"])
        with contextlib.redirect_stderr(err):
            self.assertEqual(rescan(), specs, "unchanged bytes: an equal spec, so sync leaves it running")
            self.declare()  # the installer rewriting the same declaration
            self.assertEqual(rescan(), specs, "rewritten with the same content: still equal")
            a.write_text(json.dumps(json.loads(a.read_text()), indent=4))
            edited = rescan()
        self.assertEqual([s.name for s in edited], [s.name for s in specs])
        self.assertNotEqual(edited, specs, "different bytes: a changed spec, so sync restarts it")
        sup = Supervisor([WorkerSpec(specs[0].name, SLEEPER, revision=specs[0].revision)])
        self.assertEqual(sup.sync([WorkerSpec(s.name, SLEEPER, revision=s.revision) for s in edited]),
                         ([specs[0].name], [specs[0].name]), "the supervisor restarts it through the drain")
        sup.stop(1)

    def test_the_external_owner_marker_matches_only_a_bridge_running_that_declaration(self):
        """Real pgrep: an editor on the file or a bridge for `worker.json.bak` is not an owner."""
        decl = self.declare()
        marker = self.sd.supervision_marker(specs_of(self, decl))
        procs = {}
        for label, tail in (("editor", ["vi", str(decl)]),
                            ("backup", [str(self.sd.AGENT_BRIDGE), str(decl) + ".bak"]),
                            ("bridge", [str(self.sd.AGENT_BRIDGE), str(decl)])):
            procs[label] = p = subprocess.Popen(["/bin/sh", "-c", "sleep 30; :", *tail])
            self.addCleanup(p.wait)
            self.addCleanup(p.kill)
        time.sleep(0.3)
        owner = self.sd.external_supervisor(marker)
        self.assertIsNotNone(owner)
        self.assertIn(f"pid {procs['bridge'].pid} ", owner)
        self.assertNotIn(f"pid {procs['editor'].pid} ", owner)
        self.assertNotIn(f"pid {procs['backup'].pid} ", owner)

    def test_agent_bridges_mode_waits_for_the_lock_full_mode_does_not(self):
        err = io.StringIO()
        for argv, waits in ((["--agent-bridges"], True), ([], False)):
            with mock.patch.object(self.sd, "external_supervisor", return_value=None), \
                    mock.patch.object(self.sd, "run", return_value=0) as run_, contextlib.redirect_stderr(err):
                self.sd.main(argv)
            self.assertEqual(run_.call_args[1]["wait_for_lock"], waits, argv)

    def test_full_mode_prints_a_static_workers_skip_reason(self):
        with mock.patch.object(self.sd, "_static_specs", return_value=([], ["loop-x: no interpreter configured"])):
            specs, _, _, _, err = self.main([])
        self.assertEqual(specs, [])
        self.assertIn("sparrowd: not supervised — loop-x: no interpreter configured", err.getvalue())

    def test_full_mode_still_refuses_a_gateway_bridge_supervised_elsewhere(self):
        err = io.StringIO()
        with mock.patch.object(self.sd, "external_supervisor", side_effect=lambda m: "pid 7 (1 bash wrapper)"), \
                mock.patch.object(self.sd, "run", return_value=0) as run_, contextlib.redirect_stderr(err):
            self.assertEqual(self.sd.main([]), 2)
        run_.assert_not_called()
        self.assertIn("refusing to start — remote-gateway-bridge already supervised outside sparrowd", err.getvalue())


if __name__ == "__main__":
    unittest.main()
