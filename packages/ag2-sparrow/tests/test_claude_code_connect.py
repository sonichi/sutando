#!/usr/bin/env python3
"""claude-local `connect` and `verify`: the code's redemption into the agent's seat, the agent's
own root and bridge, the agent lock around it, and the host-supervised refusal. Shared fixtures:
claude_code_fixtures.py."""
from __future__ import annotations

import argparse
import contextlib
import errno
import functools
import io
import json
import os
import plistlib
import select
import shutil
import signal
import stat
import subprocess
import sys
import threading
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

import dataclasses  # noqa: E402  (outside the scanner's Python 3.9 stdlib list)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_code_fixtures import (  # noqa: E402
    _dirs, _Resp, AGENT, AGENT_LOCK_HOLDER, AgentRootBase, Backend, BASE, BEARER, card, cb, cli, cm, CODE,
    BA, ConnectBase, CONTENDER, hold, hold_mod, inbox, L, LA, launcher, PKG, ran, records, RELAY, REPLACER, SEAT_HOLDER,
    REAL_TMUX_VERSION, seed_seat, SESSION_ID, shape_mod, sm, START_VERBS, SUCCESSOR_WRITER, task_io, tmux_pane_keys,
    TOKEN, unhold,
    UNPUBLISHED, wait_for, wait_gone,
)
from connect_undo_seams import UndoSeams, bridge_verbs  # noqa: E402


class ConnectVerify(ConnectBase):
    def checks(self, out):
        return {line.split()[1].rstrip(":"): line.split()[0] for line in out.splitlines() if line[:4] in ("PASS", "FAIL")}

    def test_verify_passes_every_part_and_fails_the_broken_one(self):
        runner = self.runner()
        self.assertEqual(self.install_with(runner)[0], 0)
        (self.home / "task-hmac.key").write_text("ab" * 32)
        self.status()
        with mock.patch.object(sm, "_run", side_effect=runner):
            rc, out, _ = self.main("verify")
        self.assertEqual(rc, 0, out)
        self.assertEqual(set(self.checks(out).values()), {"PASS"}, out)
        self.assertEqual(set(self.checks(out)), {"config", "claude", "login", "tmux", "socket", "plugin", "inbox", "results",
                                                 "hmac", "hitl", "bridge", "supervisor", "job"})
        self.assertIn(f"PASS job: {cli.SERVICE_NAME} active", out)
        self.assertIn(f"INFO session: {self.work} held by hold pid {runner.pid()}, no live claude attached (manager", out,
                      "the shared tri-state, naming the lock's holder")
        self.assertNotIn("launcher pid", out)
        self.assertIn("INFO session id: %s" % cli.read_config(cli.config_path())["SESSION_ID"], out)
        self.assertIn(f"INFO attach: {cli.COMMAND} attach --work {self.work}", out)
        self.assertIn("PASS hitl store: %s (the bridge's, beside its default store under %s)"
                      % (cli.hitl_store_root(), _dirs.state_dir()), out)
        runner.stop_session()
        rc, out, _ = self.main("verify")
        self.assertIn(f"INFO session: {self.work} free", out)
        self.assertNotEqual(rc, 0, "a configured bridge with no session job is a failed verify")
        self.assertIn(f"FAIL job: {cli.SERVICE_NAME} is unknown, so no session answers the tasks its configured bridge "
                      f"passes on: run {cli.COMMAND} install --work {self.work} again, or {cli.COMMAND} uninstall "
                      f"--work {self.work}", out)
        self.configure(HITL_STORE="/elsewhere/hitl/requirements")
        self.assertEqual(self.checks(self.main("verify")[1])["hitl"], "FAIL", "the store must be the bridge's")
        self.configure(BRIDGE_STATE_DIR="", HITL_STORE="/elsewhere/hitl/requirements-claude-local")
        out = self.main("verify")[1]
        self.assertNotIn("hitl", self.checks(out), "a config from before the key: no row derived from the store itself")
        self.assertIn(f"INFO hitl store: /elsewhere/hitl/requirements-claude-local (a config from before BRIDGE_STATE_DIR: "
                      f"the bridge's state dir is read as its parent; run {cli.COMMAND} install --work {self.work} to "
                      "record it)", out)
        self.configure()
        (self.plugin / ".claude-plugin" / "plugin.json").unlink()
        with mock.patch.object(shape_mod, "PLUGIN", self.home / "no-shipped-plugin"):
            self.assertEqual(self.checks(self.main("verify")[1])["plugin"], "FAIL", "neither dir holds a plugin")
        (self.plugin / ".claude-plugin" / "plugin.json").write_text("{}")
        self.status(ts=int(time.time()) - cli.STATUS_MAX_AGE_S - 5)
        self.assertEqual(self.checks(self.main("verify")[1])["bridge"], "FAIL", "a stale status is no bridge")
        self.status()
        (self.home / "claude-auth").write_text('{"loggedIn": false}')
        out = self.main("verify")[1]
        self.assertEqual(self.checks(out)["login"], "FAIL")
        self.assertIn("not logged in under CLAUDE_CONFIG_DIR=", out)
        (self.home / "claude-auth").unlink()
        (self.home / "claude-version").write_text("2.1.1")
        out = self.main("verify")[1]
        self.assertEqual(self.checks(out)["claude"], "FAIL")
        self.assertNotIn("login", self.checks(out), "no login check without a usable CLI")
        (self.home / "claude-version").unlink()
        cli.service_path().unlink()
        rc, out, _ = self.main("verify")
        self.assertEqual((rc, self.checks(out)["supervisor"]), (1, "FAIL"))
        with mock.patch.object(cm, "lock_held", return_value="unknown"):
            self.assertIn("INFO session: %s unknown (the lock of" % self.work, self.main("verify")[1])

    def test_a_missing_recorded_plugin_dir_falls_back_to_the_shipped_plugin(self):
        old = str(self.home / "repo" / "packages" / "ag2-sparrow" / "adapters" / "claude-code" / "ag2space-inbox")
        self.configure(PLUGIN_DIR=old)
        cfg = cli.read_config(cli.config_path())
        argv = hold_mod.claude_argv(cfg, SESSION_ID)
        self.assertEqual(argv[1:3], ["--plugin-dir", str(cli.PLUGIN)], "the in-repo dir an older install recorded")
        out = self.main("verify")[1]
        self.assertEqual(self.checks(out)["plugin"], "PASS", out)
        self.assertIn(f"PASS plugin: recorded plugin dir {old} missing; using the shipped plugin {cli.PLUGIN} "
                      f"(re-run {cli.COMMAND} install --work {self.work} to record it)", out)

    def test_a_recorded_plugin_dir_that_holds_a_plugin_stays_the_override(self):
        self.configure()
        cfg = cli.read_config(cli.config_path())
        self.assertNotEqual(self.plugin, cli.PLUGIN)
        self.assertEqual(hold_mod.claude_argv(cfg, SESSION_ID)[1:3], ["--plugin-dir", str(self.plugin)])
        out = self.main("verify")[1]
        self.assertIn(f"PASS plugin: {self.plugin}\n", out)
        self.assertNotIn("shipped plugin", out)

    def test_verify_without_a_config_says_so(self):
        rc, out, _ = self.main("verify")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL config", out)

    def test_device_env_path_is_the_shared_owners(self):
        from ag2_sparrow.adapters.hatch import cli as hatch_cli
        self.assertEqual(hatch_cli.device_env_path(), _dirs.device_env_path())
        self.assertEqual(_dirs.device_env_path(), self.relay)
        with mock.patch.dict(os.environ, {"AG2_DEVICE_ENV": "/elsewhere/relay.env"}):
            self.assertEqual(_dirs.device_env_path(), Path("/elsewhere/relay.env"))



class UndoUnits(ConnectBase):
    """The undo's refusals one by one: every unreadable, ambiguous or failed step leaves the job and says so."""

    def ident(self, **over):
        return dict({"pid": 4242, "start": "2026-01-01T00:00:00Z", "invocation": "inv-a", "since": 5}, **over)

    def test_an_unreadable_manager_is_no_identity(self):
        with mock.patch.object(sm, "_run", return_value=None):
            self.assertIsNone(cm.job_identity(L))
            with mock.patch.object(cm, "is_macos", return_value=True):
                self.assertIsNone(cm.job_identity(L))
        with mock.patch.object(sm, "_run", return_value=subprocess.CompletedProcess([], 0, "MainPID=\n", "")):
            self.assertIsNone(cm.job_identity(L), "no MainPID word")
        shown = subprocess.CompletedProcess([], 0, "InvocationID=inv-a\nMainPID=4242\nExecMainStartTimestampMonotonic=9\n", "")
        with mock.patch.object(sm, "_run", return_value=shown), mock.patch.object(task_io, "start_time", return_value=None):
            self.assertIsNone(cm.job_identity(L), "a pid whose start cannot be read")

    def test_how_the_last_run_ended_says_whether_its_manager_restarts_the_job(self):
        def systemd(code, status):
            return subprocess.CompletedProcess([], 0, f"InvocationID=inv-a\nMainPID=0\nExecMainStartTimestampMonotonic=9\n"
                                                      f"ExecMainCode={code}\nExecMainStatus={status}\n", "")
        for shown, restarts in ((systemd(1, cm.EXIT_HELD), False), (systemd(1, 74), True), (systemd(1, 0), True),
                                (systemd(2, 15), True), (systemd(0, 0), None)):
            with self.subTest(shown.stdout), mock.patch.object(sm, "_run", return_value=shown):
                self.assertEqual(cm.job_identity(L)["restarts"], restarts)
        for text, restarts in (("last exit code = 0", False), ("last exit code = 74", True),
                               ("last terminating signal = Terminated: 15", True), ("last exit code = (never exited)", None)):
            with self.subTest(text), mock.patch.object(cm, "is_macos", return_value=True), \
                    mock.patch.object(sm, "_run", return_value=subprocess.CompletedProcess([], 0, f"\tstate = not running\n\t{text}\n", "")):
                self.assertEqual(cm.job_identity(L)["restarts"], restarts)
                self.assertTrue(cm.job_identity(dataclasses.replace(L, keep_alive_on_success=None))["restarts"] in (True, None),
                                "KeepAlive=true restarts any end")

    def test_an_exact_signal_without_pidfds_and_its_failures(self):
        nofd = mock.patch.object(os, "pidfd_open", side_effect=OSError(errno.ENOSYS, "x"), create=True)
        started = mock.patch.object(task_io, "start_time", return_value="2026-01-01T00:00:00Z")
        with nofd, started, mock.patch.object(os, "kill") as kill:
            self.assertEqual(cm._signal_exact(4242, "2026-01-01T00:00:00Z", signal.SIGTERM), "")
        kill.assert_called_once_with(4242, signal.SIGTERM)
        with nofd, started, mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            self.assertEqual(cm._signal_exact(4242, "2026-01-01T00:00:00Z", signal.SIGTERM), cm._GONE)
        with nofd, started, mock.patch.object(os, "kill", side_effect=PermissionError(errno.EPERM, "Operation not permitted")):
            self.assertEqual(cm._signal_exact(4242, "2026-01-01T00:00:00Z", signal.SIGTERM),
                             "could not be signalled (Operation not permitted)")
        with nofd, mock.patch.object(task_io, "start_time", return_value=None):
            self.assertEqual(cm._signal_exact(4242, "2026-01-01T00:00:00Z", signal.SIGTERM), cm._GONE)

    def test_a_process_that_cannot_be_read_counts_as_running_until_the_bound(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("no ps")):
            self.assertTrue(cm._runs(4242, "x"))
            self.assertFalse(cm._await_exit(4242, "x", 0))

    def test_a_new_invocation_after_the_call_is_not_ours(self):
        now = self.ident(invocation="inv-b", since=50)
        with mock.patch.object(cm, "job_identity", return_value=now):
            got = cm._own_run(L, self.ident(), {"by_call": True, "t0": 10, "t1": 20})
        self.assertEqual(got, (None, "runs an invocation (pid 4242) that did not begin inside this operation's start"))

    def test_a_systemd_start_that_never_shows_a_main_pid_is_not_ours(self):
        idle = {"pid": None, "start": None, "invocation": "", "since": None}
        with mock.patch.object(cm, "job_identity", return_value=idle), mock.patch.object(cm, "START_SEEN_S", 0):
            got = cm._own_run(L, None, {"by_call": True, "t0": 0, "t1": 1})
        self.assertEqual(got, (None, "showed no process within 0 s of this operation's start"))

    def test_a_pane_holder_is_ours_only_through_a_live_launcher_run_that_recorded_its_generation(self):
        server = subprocess.Popen([sys.executable, "-c", "import subprocess, sys; p = subprocess.Popen([sys.executable, "
                                   "'-c', 'import time; time.sleep(60)']); print(p.pid, flush=True); p.wait()"],
                                  stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(lambda: (os.killpg(server.pid, signal.SIGKILL), server.wait(), server.stdout.close()))
        pane = int(server.stdout.readline())
        root = self.home / "pane-root"
        records.supervisor_path(root).parent.mkdir(parents=True)
        me = task_io.process_identity()
        gen = {"server_pid": server.pid, "server_start": wait_for(lambda: task_io.start_time(server.pid)), "pane_pid": pane}

        def proof(launcher=me, holder=pane, **over):
            records.save_supervisor(root, dict(records.empty_supervisor(), launcher=launcher, generation=dict(gen, **over)))
            return shape_mod.session_launcher(root, {"owner_pid": holder})
        self.assertEqual(proof(), os.getpid(), "the pane of our run's generation, our launcher live")
        self.assertIsNone(proof(launcher=dict(me, start="2000-01-01T00:00:00Z")), "a launcher run that is gone")
        self.assertIsNone(proof(holder=os.getpid()), "a holder that is not that generation's pane")
        self.assertIsNone(proof(server_pid=os.getpid()), "a pane that is not that server's")
        self.assertIsNone(proof(server_start="2000-01-01T00:00:00Z"), "a server that is not the recorded one")
        records.supervisor_path(root).unlink()
        self.assertIsNone(shape_mod.session_launcher(root, {"owner_pid": pane}), "no record, no proof")
        with mock.patch.object(records, "load_supervisor", side_effect=records.RecordError("torn")):
            self.assertIsNone(shape_mod.session_launcher(root, {"owner_pid": pane}))

    def test_the_undo_outside_the_agent_lock_stops_nothing(self):
        p = {"shape": L, "root": str(self.work), "owned": True, "why": "", "unit": b"", "undo": [], "ident": self.ident()}
        with mock.patch.object(cm, "_retire_run") as retire:
            line = cm._undo_start(p)
        retire.assert_not_called()
        self.assertIn("this operation does not hold the agent lock, so nothing was stopped", line)
        self.assertIn(cm.stop_command(L), line)

    def test_stop_started_with_nothing_recorded_or_not_ours(self):
        self.work.mkdir(parents=True)
        with cm.agent_lock(str(self.work)):
            with mock.patch.dict(cm._STARTED, clear=True):
                self.assertIn("no job started by this connect was recorded", cm.stop_started(L, str(self.work)))
                cm._STARTED[L.name] = {"shape": L, "root": str(self.work), "owned": False,
                                       "why": "was already running when this operation asked for it"}
                self.assertIn("so it is not this operation's", cm.stop_started(L, str(self.work)))
                self.assertIn(L.name, cm._STARTED, "a run that is not ours stays recorded as it is")

    def _run(self, retires=True):
        code = ("import signal, sys, time\n"
                + ("signal.signal(signal.SIGURG, lambda *a: sys.exit(75))\n" if retires else "")
                + "print('ready', flush=True); time.sleep(60)")
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.kill(), child.wait(), child.stdout.close()) if child.poll() is None else None)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        return child, {"pid": child.pid, "start": wait_for(lambda: task_io.start_time(child.pid)), "invocation": ""}

    def test_a_run_asked_to_retire_ends_for_good_and_its_manager_is_never_named(self):
        child, ident = self._run()
        idle = {"pid": None, "start": None, "invocation": "", "since": None, "restarts": False}
        with mock.patch.object(cm, "job_identity", return_value=idle), mock.patch.object(cm, "stop_supervisor") as stop, \
                mock.patch.object(cm, "_signal_exact", wraps=cm._signal_exact) as sig:
            self.assertEqual(cm._retire_run(L, ident), "")
        self.assertEqual(child.wait(timeout=10), cm.EXIT_HELD, "the exit neither manager restarts")
        self.assertEqual({c.args[2] for c in sig.call_args_list}, {cm.RETIRE_SIGNAL})
        stop.assert_not_called()

    def test_a_run_that_does_not_retire_is_asked_again_then_left(self):
        child, ident = self._run(retires=False)
        quick = dataclasses.replace(L, stop_timeout_s=1)
        with mock.patch.object(cm, "_signal_exact", wraps=cm._signal_exact) as sig, \
                mock.patch.object(cm, "stop_supervisor") as stop:
            self.assertEqual(cm._retire_run(quick, ident), f"pid {child.pid} did not retire within 1 s")
        self.assertGreater(len(sig.call_args_list), 1, "a request sent before its handler was in place is repeated")
        self.assertIsNone(child.poll(), "a run that does not retire is left, never killed")
        stop.assert_not_called()

    def test_a_run_seen_after_ours_retired_is_left(self):
        child, ident = self._run()
        again = {"pid": 999999, "start": "x", "invocation": "", "since": None}
        with mock.patch.object(cm, "job_identity", return_value=again), mock.patch.object(cm, "stop_supervisor") as stop:
            line = cm._retire_run(L, ident)
        self.assertEqual(line, f"pid {child.pid} retired, but the job runs again as pid 999999, which this operation "
                               "did not start")
        stop.assert_not_called()

    def test_a_run_that_ended_asking_to_be_restarted_is_never_reported_stopped(self):
        for restarts, said in ((True, "ended with an exit its manager restarts, not as a retirement, so the job starts "
                                      "again after its restart delay"),
                               (None, "ended, but how it ended cannot be read, so its manager may start the job again")):
            with self.subTest(restarts=restarts):
                child, ident = self._run()
                ended = {"pid": None, "start": None, "invocation": "", "since": None, "restarts": restarts}
                with mock.patch.object(cm, "job_identity", return_value=ended), mock.patch.object(cm, "stop_supervisor") as stop:
                    self.assertEqual(cm._retire_run(L, ident), f"pid {child.pid} {said}")
                stop.assert_not_called()

    def test_each_failed_retire_step_leaves_the_job_and_says_why(self):
        ident = self.ident()
        with mock.patch.object(cm, "_signal_exact", return_value=cm._OTHER):
            self.assertEqual(cm._retire_run(L, ident), f"pid 4242 {cm._OTHER}; nothing was signalled")
        with mock.patch.object(cm, "_signal_exact", return_value=""), mock.patch.object(cm, "_await_exit", return_value=True), \
                mock.patch.object(cm, "job_identity", return_value=None):
            self.assertEqual(cm._retire_run(L, ident), "its state after pid 4242 retired cannot be read")

    def test_retiring_is_a_run_s_own_exit_through_the_launchd_shim(self):
        state = cm.stop_state()
        self.assertEqual(cm.retired(state, 143), 143, "an ordinary stop keeps its code")
        state["retire"] = True
        self.assertEqual(cm.retired(state, 143), cm.EXIT_HELD)
        self.assertEqual(cm.retired(state, cm.EXIT_RETRY), cm.EXIT_RETRY, "a run that asks to be retried still is")
        child = ("import signal, sys, time; sys.path.insert(0, %r)\n"
                 "from ag2_sparrow import connect_common as cm\n"
                 "state = cm.stop_state(); cm.stop_handlers(state)\n"
                 "import os; print('ready', os.getpid(), flush=True)\n"
                 "while state['sig'] is None: time.sleep(0.05)\n"
                 "sys.exit(cm.retired(state, 128 + state['sig']))") % str(PKG)
        shim = ("import sys; sys.path.insert(0, %r); from ag2_sparrow import connect_common as cm\n"
                "sys.exit(cm.run_launchd([sys.executable, '-c', %r]))") % (str(PKG), child)
        p = subprocess.Popen([sys.executable, "-c", shim], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.kill(), p.wait()) if p.poll() is None else None)
        ready = p.stdout.readline().split()
        self.assertEqual(ready[:1], ["ready"])
        self.addCleanup(lambda: wait_gone(int(ready[1]), 0) or os.kill(int(ready[1]), signal.SIGKILL))
        deadline = time.monotonic() + 10
        while p.poll() is None and time.monotonic() < deadline:
            os.kill(p.pid, cm.RETIRE_SIGNAL)            # until the shim's forwarding is in place
            time.sleep(0.1)
        p.stdout.close()
        self.assertEqual(p.wait(timeout=10), 0, "the shim's 0, which SuccessfulExit=false does not restart")


class ConnectCode(UndoSeams, AgentRootBase):
    """`connect --base --code`: preflight before the code is spent, one redemption, the seat written by its
    owner, then install's own path with the seat's .env; the bearer never in argv, output, unit or config."""

    local, tmux_tree = LA, True

    def seam_fake(self):
        return self.bridged()

    def backend(self):
        return Backend()

    def install_seat(self, fake):
        seed_seat(cli.bridge_home(self.root), BEARER)
        return self.install_default(fake, "--token-env-file", str(cli.bridge_home(self.root) / "channels" / "ag2space" / ".env"))

    def test_an_exact_signal_reaches_only_the_process_with_the_recorded_start(self):
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (child.kill(), child.wait()) if child.poll() is None else None)
        start = wait_for(lambda: task_io.start_time(child.pid))
        self.assertEqual(cm._signal_exact(child.pid, "2000-01-01T00:00:00Z", signal.SIGTERM), cm._OTHER)
        self.assertIsNone(child.poll(), "a pid whose start differs is never signalled")
        self.assertEqual(cm._signal_exact(child.pid, start, signal.SIGTERM), "")
        self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)
        self.assertEqual(cm._signal_exact(child.pid, start, signal.SIGTERM), cm._GONE)

    def test_a_holder_record_that_cannot_be_written_releases_the_agent_lock_and_refuses(self):
        self.root.mkdir(parents=True)
        root = str(self.root)
        full = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(cm, "_say_holder", side_effect=full):
            with self.assertRaises(cm.Busy) as refused:
                cm.agent_lock(root).__enter__()
            self.assertIn("its holder could not be recorded (No space left on device); it was released", str(refused.exception))
            self.assertFalse(cm.agent_lock_held(root))
            self.assertEqual(cm.agent_lock_busy(root), "", "released at once, not at exit")
            fd, why = cm.launch_admission(root, wait_s=0)
            self.assertIsNone(fd)
            self.assertIn("its holder could not be recorded", why)
            self.assertEqual(cm.agent_lock_busy(root), "")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cm.install(L, {}, root), 1)
            self.assertIn("its holder could not be recorded", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())
        with cm.agent_lock(root):
            self.assertTrue(cm.agent_lock_held(root), "the next operation takes it")

    def connect(self, backend, fake=None, *extra, sleeps=None, stdin=None, base=BASE, code_args=("--code", "-"),
                plugin=True):
        fake = fake or self.bridged()
        argv = ["connect", "--base", base, "--agent", AGENT, *code_args, *(("--plugin-dir", str(self.plugin)) if plugin else ()),
                "--claude-config-dir",
                str(self.home / ".claude-sutando"), "--hmac-key-file", str(self.home / "task-hmac.key"), *extra]
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(CODE + "\n") if stdin is None else stdin
        with mock.patch.object(sm, "_run", side_effect=fake), \
                mock.patch.object(urllib.request, "urlopen", side_effect=backend), \
                mock.patch.object(cm, "redeem_code", side_effect=functools.partial(
                    cm.redeem_code, sleep=lambda s: (sleeps if sleeps is not None else []).append(s))), \
                mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def seat(self):
        return cli.bridge_home(self.root) / "channels" / "ag2space"

    def test_a_code_is_redeemed_once_and_the_seat_feeds_the_installed_bridge(self):
        self.relay.unlink()     # a fresh host: no device env, nothing but the code
        backend = Backend()
        rc, out, err = self.connect(backend)
        self.assertEqual(rc, 0, err)
        self.assertEqual(backend.calls, [(BASE + "/connect/poll", {"device_code": CODE}, "POST")], "read from stdin")
        self.assertNotIn("warning", err, "a code from stdin is not on the command line")
        seat = self.seat()
        self.assertEqual(sorted(p.name for p in seat.iterdir()), [".env", "token"])
        for name in (".env", "token"):
            self.assertEqual(stat.S_IMODE((seat / name).stat().st_mode), 0o600, name)
        self.assertEqual((seat / "token").read_text(), f"{RELAY}|{BEARER}\n")
        values = dict(ln.split("=", 1) for ln in (seat / ".env").read_text().splitlines())
        self.assertEqual((values["AGENT_MXID"], values["REMOTE_TASK_TIER"], values["REMOTE_TASK_TOKEN_FILE"]),
                         (AGENT, "owner", str(seat / "token")))
        self.assertRegex(values["GATEWAY_INSTANCE"], r"^[0-9a-f]{32}$")
        cfg = self.cfg()
        self.assertEqual((cfg["ROOT"], cfg["TOKEN_ENV_FILE"], cfg["AGENT_ID"]), (str(self.root), str(seat / ".env"), AGENT))
        unit = cm.service_path(BA).read_text()
        self.assertIn('Environment="REMOTE_TASK_SEAT=1"\n', unit)
        self.assertIn(f'Environment="REMOTE_TASK_TOKEN_FILE={seat / "token"}"\n', unit)
        self.assertIn("setup: built-in", out)
        for text in (out, err, unit, cli.config_path(LA).read_text(), (seat / ".env").read_text()):
            self.assertNotIn(BEARER, text)

    def seated_by_another_adapter(self):
        """The agent's root and seat as another adapter left them: its credential in the seat."""
        self.root.mkdir(parents=True, exist_ok=True)
        seed_seat(cli.bridge_home(self.root), "other-bearer-PPPP6666")
        return {p: p.read_bytes() for p in sorted(self.seat().iterdir())}

    def test_a_root_another_adapter_holds_is_refused_before_the_seat_is_written(self):
        before = self.seated_by_another_adapter()
        holder = hold(self.root, "codex-local")
        try:
            backend = Backend()
            rc, _out, err = self.connect(backend)
        finally:
            unhold(holder)
        self.assertEqual({p: p.read_bytes() for p in sorted(self.seat().iterdir())}, before, "byte-identical")
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertIn(f"{self.root} is held by launcher pid {holder.pid}; stop what holds it", err)
        self.assertIn("the code is spent and nothing was written", err)
        self.assertEqual(len(backend.calls), 1, "the root is known only from the redemption's agent id")
        self.assertFalse(cli.config_path(LA).exists())
        self.assertFalse(cm.service_path(BA).exists())

    def test_a_free_root_passes_installs_own_check_and_connect_proceeds(self):
        self.seated_by_another_adapter()
        checked = []
        real = cm.under_root
        with mock.patch.object(cm, "under_root",
                               side_effect=lambda shape, root, act, **kw: checked.append((shape.name, root)) or real(shape, root, act, **kw)):
            rc, _out, err = self.connect(Backend())
        self.assertEqual(rc, 0, err)
        self.assertEqual(checked, [(cli.SHAPE, str(self.root))], "asked once, before the seat")
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|{BEARER}\n")

    def files(self):
        """Every file under the test home, by bytes, but the empty operation lock files: what a refused connect
        may not change."""
        return {str(p): p.read_bytes() for p in sorted(self.home.rglob("*")) if p.is_file() and p.suffix not in (".oplock", ".fence") and p.name != cm.SEAT_LOCK and p.parent.name != ".locks"}

    def assert_refused_untouched(self, fake, why, spent="the code is spent and nothing was written", unspent=False):
        """connect refuses before the seat, the agent's seat byte-identical and no file anywhere changed;
        `unspent`: refused before the code is redeemed at all."""
        seat = self.seated_by_another_adapter()
        before, backend = self.files(), Backend()
        rc, out, err = self.connect(backend, fake)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertEqual({p: p.read_bytes() for p in sorted(self.seat().iterdir())}, seat, "byte-identical")
        self.assertEqual(self.files(), before, "nothing else was written")
        self.assertNotIn("connected:", out)
        self.assertIn(why, err)
        self.assertIn("the code was not used" if unspent else spent, err)
        self.assertEqual(len(backend.calls), 0 if unspent else 1)

    def test_a_job_active_for_another_root_does_not_vouch_for_a_held_root(self):
        a = self.home / "agent-a"
        fake = self.bridged(root=a, shape=L)
        rc, _out, err = self.install_default(fake, "--work", str(a))
        self.assertEqual(rc, 0, err)
        self.assertEqual(cm.job_for(L, AGENT), L, "the explicit root's job records the host's agent: it is that agent's")
        self.root.mkdir(parents=True)
        holder = hold(self.root, "codex-local")
        try:
            self.assert_refused_untouched(fake, f"{cm.job_name(L)} is running for", unspent=True)
        finally:
            unhold(holder)

    def test_an_active_job_that_cannot_be_proven_to_manage_the_root_fails_closed(self):
        cases = (("no config", None, L.name, "active for no recorded work root"),
                 ("no recorded root", "", L.name, "active for no recorded work root"),
                 ("unparsable config", "unparsable", L.name, "active for no recorded work root"),
                 ("another holder, this root", "root", "codex-local", "is held by codex-local pid"),
                 ("this adapter, another root", "elsewhere", L.name, "is held by claude-local pid"),
                 ("this adapter and root, another install", "root", (L.name, "/elsewhere/site-packages/ag2_sparrow"),
                  "of another install"))
        for name, root, adapter, why in cases:
            with self.subTest(name):
                shutil.rmtree(self.agents, ignore_errors=True)
                root = str(self.home / "agent-a") if root == "elsewhere" else root
                adapter, install = adapter if isinstance(adapter, tuple) else (adapter, "")
                cli.config_path(LA).unlink(missing_ok=True)
                if root is not None:
                    self.configure(LA, ROOT=str(self.root) if root in ("root", "unparsable") else root)
                if root == "unparsable":
                    cli.config_path(LA).write_text(cli.config_path(LA).read_text() + "ROOT='unclosed\n")
                fake = self.bridged(adapter=adapter, install=install)
                self.root.mkdir(parents=True)
                fake.runner.start_session()
                try:
                    self.assert_refused_untouched(fake, f"{cm.job_name(LA)} is running for", unspent=True)
                finally:
                    fake.runner.stop_session()

    def test_an_independent_holder_of_this_shape_on_the_root_is_refused_while_the_job_serves_another(self):
        a = self.home / "agent-a"
        fake = self.bridged(root=a, shape=L)
        rc, _out, err = self.install_default(fake, "--work", str(a))
        self.assertEqual(rc, 0, err)
        self.assertEqual(cm.job_for(L, AGENT), L, "the explicit root's job records the host's agent: it is that agent's")
        self.root.mkdir(parents=True)
        holder = hold(self.root, L.name)
        try:
            self.assert_refused_untouched(fake, f"{cm.job_name(L)} is running for", unspent=True)
        finally:
            unhold(holder)

    def test_a_stale_record_of_this_shape_does_not_vouch_for_a_successor_that_has_not_published(self):
        self.configure(LA, ROOT=str(self.root))
        self.root.mkdir(parents=True)
        dead = hold(self.root, L.name)
        dead.kill()
        dead.wait(timeout=30)
        dead.stdin.close()
        dead.stdout.close()
        fake = self.bridged()
        fake.runner.session = hold(self.root, script=UNPUBLISHED)   # the manager reads active while it lives
        try:
            rec = task_io.lock_holder(str(self.root))
            self.assertEqual((rec["adapter"], rec["pid"], rec["live"]), (L.name, dead.pid, False))
            self.assertIn("is held by a holder with no live record", cm._managed_here(L, str(self.root)),
                          "install's own check needs the record to be live")
            self.assert_refused_untouched(fake, f"{cm.job_name(LA)} is running for", unspent=True)
        finally:
            fake.runner.stop_session()

    def replace_root(self):
        """A real process parks the root and its seat, seats a fresh root at the same paths with another
        token and holds it with the other adapter: the held directories are no longer what the paths name."""
        p = subprocess.Popen([sys.executable, "-c", REPLACER % (str(PKG), str(self.root), RELAY, AGENT, "codex-local")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def assert_replaced_and_refused(self, target, wrap, why):
        """connect with connect_common.`target` wrapped by `wrap(real, replace)`: it refuses, the replacing
        root's seat keeps its own token, and the parked seat holds its previous bytes, never the new bearer."""
        before = {p.name: p.read_bytes() for p in self.seated_by_another_adapter()}
        late, inode = [], []

        def replace():
            late.append(self.replace_root())
            inode.append(os.stat(self.seat() / "token").st_ino)
        try:
            if target == "os.replace":
                with mock.patch("os.replace", side_effect=wrap(os.replace, replace)):
                    rc, out, err = self.connect(Backend())
            else:
                with mock.patch.object(cm, target, side_effect=wrap(getattr(cm, target), replace)):
                    rc, out, err = self.connect(Backend())
        finally:
            for p in late:
                unhold(p)
        self.assertEqual(len(late), 1, "replaced once")
        self.assertEqual(os.stat(self.seat() / "token").st_ino, inode[0], "the replacing seat's token never replaced")
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertNotIn("connected:", out)
        self.assertIn(why, err)
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|replacer-bearer-RRRR7777\n",
                         "the replacing root's seat is untouched")
        parked = Path(str(self.seat().parent.parent) + ".parked") / "channels" / "ag2space"
        self.assertEqual({p.name: p.read_bytes() for p in parked.iterdir()}, before, "the parked seat as it was")

    def test_a_root_replaced_after_connect_holds_it_keeps_both_seats(self):
        def wrap(real, replace):
            return lambda root, adapter: (real(root, adapter), replace())[0]
        self.assert_replaced_and_refused("acquire_lock", wrap, "is no longer what connect holds")

    def test_a_root_replaced_between_the_check_and_the_write_keeps_both_seats(self):
        def wrap(real, replace):
            calls = []

            def checked(owner, root):
                got = real(owner, root)
                if not calls:
                    replace()
                calls.append(got)
                return got
            return checked
        self.assert_replaced_and_refused("root_is_held", wrap, "was replaced while the seat was written")

    def test_a_write_that_fails_partway_after_a_replacement_puts_the_held_seat_back(self):
        def wrap(real, replace):
            failed = []

            def committing(src, dst, *a, **kw):
                if os.path.basename(dst) == ".env" and not failed:
                    failed.append(replace())
                    raise PermissionError(13, "Permission denied")
                return real(src, dst, *a, **kw)
            return committing
        self.assert_replaced_and_refused("os.replace", wrap, "could not be written (Permission denied); its "
                                                             "previous files were put back")

    def test_a_root_replaced_after_the_write_keeps_both_seats(self):
        def wrap(real, replace):
            calls = []

            def checked(owner, root):
                if calls:
                    replace()
                calls.append(True)
                return real(owner, root)
            return checked
        self.assert_replaced_and_refused("root_is_held", wrap, "was replaced while the seat was written")

    def test_a_symlinked_seat_path_is_refused_and_nothing_is_written_through_it(self):
        elsewhere = self.home / "elsewhere-seat"
        seed_seat(elsewhere, "elsewhere-bearer-SSSS8888")
        target = {p: p.read_bytes() for p in elsewhere.rglob("*") if p.is_file()}
        home = cli.bridge_home(self.root)
        for name in ("seat dir", "channel dir", "token"):
            with self.subTest(name):
                home.unlink() if home.is_symlink() else shutil.rmtree(home, ignore_errors=True)
                self.root.mkdir(parents=True, exist_ok=True)
                if name == "seat dir":
                    link, to = home, elsewhere
                else:
                    seed_seat(home, "other-bearer-PPPP6666")
                    link = self.seat() if name == "channel dir" else self.seat() / "token"
                    to = elsewhere / link.relative_to(home)
                    shutil.rmtree(link) if link.is_dir() else link.unlink()
                link.symlink_to(to)
                rc, out, err = self.connect(Backend())
                self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
                self.assertNotIn("connected:", out)
                self.assertIn("the seat is unchanged; the code is spent", err)
                self.assertEqual(os.readlink(link), str(to), "the link itself is left as it was")
                self.assertEqual({p: p.read_bytes() for p in elsewhere.rglob("*") if p.is_file()}, target,
                                 "nothing written through the link")

    def test_a_successor_on_a_replaced_root_keeps_its_seat_write_over_the_older_rollback(self):
        self.seated_by_another_adapter()
        real, successor, early = cm.root_is_held, [], []

        def checked(owner, root):
            if successor or not hasattr(checked, "pre"):
                checked.pre = True
                return real(owner, root)
            p = subprocess.Popen([sys.executable, "-c", SUCCESSOR_WRITER % (str(PKG), str(self.root), RELAY, AGENT,
                                                                             "codex-local")],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            successor.append(p)
            self.assertEqual(p.stdout.readline().strip(), "held")
            if select.select([p.stdout], [], [], 3)[0]:     # the older writer's seat lock holds it off
                early.append(p.stdout.readline().strip())
            return real(owner, root)
        try:
            with mock.patch.object(cm, "root_is_held", side_effect=checked):
                rc, out, err = self.connect(Backend())
            p = successor[0]
            line = early[0] if early else (p.stdout.readline().strip() if select.select([p.stdout], [], [], 30)[0]
                                           else "no answer")
        finally:
            for p in successor:
                unhold(p)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertNotIn("connected:", out)
        self.assertIn("was replaced while the seat was written", err)
        self.assertEqual(line, "written ok", "the successor's own write succeeded")
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|successor-bearer-UUUU9999\n",
                         "the successor's credential survives the older writer's rollback")
        self.assertEqual(early, [], "the successor waited for the older writer's seat lock")

    def test_the_rollback_leaves_a_file_that_no_longer_holds_what_this_write_put_there(self):
        self.seated_by_another_adapter()
        real, late, newer = cm.root_is_held, [], f"{RELAY}|newer-bearer-VVVV0000\n"

        def checked(owner, root):
            if hasattr(checked, "pre") and not late:
                late.append(self.replace_root())
                (Path(str(self.seat().parent.parent) + ".parked") / "channels" / "ag2space" / "token").write_text(newer)
            checked.pre = True
            return real(owner, root)
        try:
            with mock.patch.object(cm, "root_is_held", side_effect=checked):
                rc, out, err = self.connect(Backend())
        finally:
            for p in late:
                unhold(p)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        parked = Path(str(self.seat().parent.parent) + ".parked") / "channels" / "ag2space"
        self.assertEqual((parked / "token").read_text(), newer, "bytes this write did not put there are left")
        self.assertNotIn(BEARER, (parked / ".env").read_text() + (parked / "token").read_text())

    def test_a_fifo_seat_file_is_refused_at_once_and_left_as_it_is(self):
        self.seated_by_another_adapter()
        token = self.seat() / "token"
        token.unlink()
        os.mkfifo(token)
        got = []
        t = threading.Thread(target=lambda: got.append(self.connect(Backend())), daemon=True)
        t.start()
        t.join(10)
        if t.is_alive():
            os.close(os.open(token, os.O_WRONLY))     # end the read connect is stuck in
            t.join(30)
            self.fail("connect hung reading the FIFO seat token")
        rc, out, err = got[0]
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertNotIn("connected:", out)
        self.assertIn("token is not a regular file", err)
        self.assertTrue(stat.S_ISFIFO(os.lstat(token).st_mode), "the FIFO is left as it is")

    def held_root(self):
        """The agent's seated root, held by this process as connect holds it."""
        self.seated_by_another_adapter()
        owner = task_io.acquire_lock(str(self.root), cm.CONNECT_HOLDER)
        self.addCleanup(task_io.release_lock, owner)
        return owner

    def test_write_seat_held_refuses_a_fifo_token_within_a_bound_and_no_writer(self):
        owner, token = self.held_root(), self.seat() / "token"
        token.unlink()
        os.mkfifo(token)
        got = []
        t = threading.Thread(target=lambda: got.append(cm.write_seat_held(
            owner, str(self.root), cli.bridge_home(self.root), {"relay_url": RELAY, "bearer": BEARER, "agent_id": AGENT})),
            daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "write_seat_held returned within 5 s, with no writer on the FIFO")
        self.assertEqual(got[0][0], "")
        self.assertIn("(token is not a regular file); the seat is unchanged", got[0][1])
        self.assertTrue(stat.S_ISFIFO(os.lstat(token).st_mode), "the FIFO is left as it is")

    def test_write_seat_held_reads_a_seat_file_up_to_the_bound_and_refuses_one_over_it(self):
        owner, bound = self.held_root(), 64 * 1024
        for size, ok in ((bound - 1, True), (bound, True), (bound + 1, False)):
            with self.subTest(size):
                (self.seat() / "token").write_bytes(b"x" * size)
                seat_env, why = cm.write_seat_held(owner, str(self.root), cli.bridge_home(self.root),
                                                   {"relay_url": RELAY, "bearer": BEARER, "agent_id": AGENT})
                token = (self.seat() / "token").read_bytes()
                if ok:
                    self.assertEqual((why, token), ("", f"{RELAY}|{BEARER}\n".encode()))
                    self.assertEqual(seat_env, str(self.seat() / ".env"))
                else:
                    self.assertEqual(seat_env, "")
                    self.assertIn(f"token is larger than {bound} bytes", why)
                    self.assertEqual(token, b"x" * size, "the oversized file is left as it is")

    def running_job(self):
        """The agent connected once: its managed launcher live on the root."""
        fake = self.bridged()
        self.assertEqual(self.connect(Backend(), fake)[0], 0)
        self.assertIsNotNone(fake.runner.pid())
        return fake

    def assert_job_runs(self, fake, pid=None):
        rec = task_io.lock_holder(str(self.root))
        self.assertTrue(rec.get("live") and rec.get("adapter") == L.name, rec)
        self.assertEqual(rec["pid"], fake.runner.pid(), "the managed launcher holds the root")
        if pid is not None:
            self.assertEqual(fake.runner.pid(), pid, "the same launcher, never stopped")

    def seat_lock_holder(self):
        p = subprocess.Popen([sys.executable, "-c", SEAT_HOLDER, str(cli.bridge_home(self.root) / ".seat.lock")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def test_a_seat_that_cannot_be_written_refuses_before_anything_is_written_or_started(self):
        for name in ("fifo token", "oversized token", "seat lock held", "seat lock held, no channel dir"):
            with self.subTest(name):
                self.seated_by_another_adapter()
                token, holder, fake = self.seat() / "token", None, self.bridged()
                try:
                    if name == "fifo token":
                        token.unlink()
                        os.mkfifo(token)
                    elif name == "oversized token":
                        token.write_bytes(b"x" * (64 * 1024 + 1))
                    else:
                        if name.endswith("no channel dir"):
                            shutil.rmtree(self.seat().parent)
                        holder = self.seat_lock_holder()
                    with mock.patch.object(cm, "SEAT_WAIT_S", 0.3):
                        rc, out, err = self.connect(Backend(), fake)
                    self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
                    self.assertNotIn("connected:", out)
                    self.assertIn("the seat is unchanged", err)
                    self.assertIsNone(fake.runner.pid(), "nothing was started")
                finally:
                    if holder is not None:
                        unhold(holder)
                    shutil.rmtree(cli.bridge_home(self.root), ignore_errors=True)

    def test_connect_with_resume_session_hosts_that_existing_conversation(self):
        resumed = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"
        projects = self.home / ".claude-sutando" / "projects" / "-Users-owner-long-project"
        projects.mkdir(parents=True)
        (projects / (resumed + ".jsonl")).write_text("{}\n")
        rc, out, err = self.connect(Backend(), None, "--resume-session", resumed)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"resuming the existing conversation {resumed}", out)
        self.assertEqual(self.cfg()["SESSION_ID"], resumed)

    def test_a_resume_session_not_applied_exits_79_and_leaves_the_job_and_its_bridge_installed(self):
        resumed = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"
        fake = self.bridged()
        self.assertEqual(self.install_seat(fake)[0], 0)
        first = self.cfg()["SESSION_ID"]
        projects = self.home / ".claude-sutando" / "projects" / "-Users-owner-long-project"
        projects.mkdir(parents=True)
        (projects / (resumed + ".jsonl")).write_text("{}\n")
        live = {"server_pid": os.getpid(), "server_start": task_io.start_time(os.getpid()), "session_id": "$1",
                "session_created": 1, "pane_id": "%0", "pane_pid": 1, "start_command": "x", "nonce": "0" * 32}
        self.assertEqual(inbox.prepare_root(str(self.root)), "")
        records.save_supervisor(str(self.root), dict(records.empty_supervisor(), generation=live))
        rc, out, err = self.install_default(fake, "--token-env-file", str(self.seat() / ".env"),
                                            "--resume-session", resumed)
        self.assertEqual(rc, cli.EXIT_RESUME_NOT_APPLIED, out + err)
        self.assertNotIn(rc, (0, 1, cli.EXIT_UNUSABLE, cm.EXIT_HELD, cm.EXIT_RETRY), "a code of its own")
        self.assertIn(f"error: --resume-session {resumed} was not applied", err)
        cfg = self.cfg()
        self.assertEqual((cfg["SESSION_ID"], cfg["ROOT"]), (first, str(self.root)), "the config keeps the old id")
        self.assert_job_runs(fake)
        self.assertTrue(cm.service_path(BA).read_text().startswith(BA.marker), "the bridge stays installed")
        self.assertNotIn("bridge", err.replace(f"--resume-session {resumed} was not applied", ""),
                         "nothing about the bridge being stopped or withdrawn")

    def test_a_resume_session_without_its_transcript_is_refused_before_the_code_is_spent_or_the_job_stopped(self):
        fake = self.running_job()
        pid = fake.runner.pid()
        backend = Backend()
        resumed = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"
        rc, out, err = self.connect(backend, fake, "--resume-session", resumed)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertIn(f"no conversation {resumed}", err)
        self.assertIn("the code was not used", err)
        self.assertEqual(backend.calls, [], "the code is not spent")
        self.assert_job_runs(fake, pid)

    def test_an_unusable_hmac_key_is_refused_before_the_code_is_spent_or_the_job_stopped(self):
        fake = self.running_job()
        pid = fake.runner.pid()
        (self.home / "task-hmac.key").write_text("not-a-key")
        backend = Backend()
        rc, out, err = self.connect(backend, fake)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertIn("needs a usable key", err)
        self.assertIn("the code was not used", err)
        self.assertEqual(backend.calls, [], "the code is not spent")
        self.assertNotIn("stopped:", out)
        self.assert_job_runs(fake, pid)

    def test_a_credential_that_would_render_past_the_read_bound_is_refused_unwritten(self):
        owner = self.held_root()
        before = (self.seat() / "token").read_bytes()
        seat_env, why = cm.write_seat_held(owner, str(self.root), cli.bridge_home(self.root),
                                           {"relay_url": RELAY, "bearer": "b" * (64 * 1024), "agent_id": AGENT})
        self.assertEqual(seat_env, "")
        self.assertIn("the seat's token would be larger than 65536 bytes", why)
        self.assertEqual((self.seat() / "token").read_bytes(), before)

    def test_a_stable_root_through_the_same_seam_connects(self):
        self.seated_by_another_adapter()
        seen, real = [], cm.acquire_lock
        with mock.patch.object(cm, "acquire_lock", side_effect=lambda root, adapter: seen.append(root) or real(root, adapter)):
            rc, out, err = self.connect(Backend())
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen, [str(self.root)])
        self.assertIn("connected: ", out)
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|{BEARER}\n")

    def test_a_holder_that_takes_the_root_after_the_clean_check_keeps_the_seat(self):
        self.root.mkdir(parents=True)
        late, real = [], cm._free_root_problem

        def checked_then_taken(root):
            got = real(root)
            if not late:
                late.append(hold(self.root, "codex-local"))
            return got
        try:
            with mock.patch.object(cm, "_free_root_problem", side_effect=checked_then_taken):
                self.assert_refused_untouched(None, f"{self.root} was taken by another process first")
        finally:
            for p in late:
                unhold(p)
        self.assertEqual(len(late), 1)

    def test_a_failed_install_after_connect_names_the_seat_that_recovers_the_spent_code(self):
        seat = cli.bridge_home(self.root) / "channels" / "ag2space" / ".env"
        rerun = (f"{cli.COMMAND} install --agent {AGENT} --plugin-dir {self.plugin} --claude-config-dir "
                 f"{self.home / '.claude-sutando'} "
                 f"--hmac-key-file {self.home / 'task-hmac.key'} --permission-mode plan --token-env-file {seat}")
        with mock.patch.object(cm, "console_script",
                               side_effect=lambda shape: "" if shape == BA else str(self.bin / shape.command)):
            rc, _out, err = self.connect(Backend(), None, "--permission-mode", "plan")
        self.assertEqual(rc, cli.EXIT_UNUSABLE, err)
        self.assertIn("the session is installed but its bridge is not: ag2-sparrow was not found beside", err)
        self.assertIn(f"the code is spent and its credential is in the seat {seat}; once the line above is fixed, run "
                      f"{rerun} (no new code is needed)", err)

    def test_a_bridge_wiring_failure_after_install_unwinds_this_attempt_and_restarts_nothing(self):
        fake = self.running_job()
        fake.runner.stop_session()
        before = len(fake.runner.calls)
        with mock.patch.object(cm, "console_script",
                               side_effect=lambda shape: "" if shape == BA else str(self.bin / shape.command)):
            rc, out, err = self.connect(Backend(), fake)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertIn("its bridge is not: ag2-sparrow was not found beside", err)
        starts = [c for c in fake.runner.calls[before:] if fake.runner.verb(c) in START_VERBS]
        self.assertEqual(len(starts), 1, "install started the job once; nothing started it again")
        self.assertNotIn("next:", out)
        self.assertIn(f"; this attempt was undone: {cm.job_name(LA)}, the job its install started, was stopped", err)
        self.assertIsNone(fake.runner.pid(), "the job this attempt started is not left running")

    def test_a_job_started_after_the_seat_write_is_left_running_and_connect_refuses(self):
        fake = self.running_job()
        fake.runner.stop_session()
        rotated = Backend(answer={"relay_url": RELAY, "bearer": "rotated-bearer-OOOO5555", "agent_id": AGENT})
        real, raced = cm.install, []

        def started_then_installed(*a, **kw):
            fake.runner.start_session()          # the manager, sparrowd or the owner starts it in the hand-off
            raced.append((fake.runner.pid(), len(fake.runner.calls)))
            return real(*a, **kw)
        with mock.patch.object(cm, "install", side_effect=started_then_installed):
            rc, out, err = self.connect(rotated, fake)
        pid, mark = raced[0]
        self.assertNotEqual(rc, 0, out + err)
        self.assertEqual(fake.runner.pid(), pid, "the raced launcher is the one still running")
        self.assert_job_runs(fake, pid)
        verbs = [fake.runner.verb(c) for c in fake.runner.calls[mark:]]
        self.assertFalse(set(verbs) & {"stop", "disable", "restart", "bootout", "kickstart", "start", "bootstrap"},
                         verbs)
        self.assertNotIn("stopped:", out)
        self.assertIn("connect stops nothing, so a running job keeps the seat it has read", err)
        self.assertEqual(len(rotated.calls), 1)
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|rotated-bearer-OOOO5555\n")
        self.assertIn("the code is spent and its credential is in the seat", err)

    def test_a_job_started_between_connects_check_and_its_start_is_kept_not_restarted(self):
        fake = self.running_job()
        fake.runner.stop_session()
        real, raced = cm.start_supervisor, []

        def raced_then_started(shape, **kw):
            if shape == LA and not raced:
                fake.runner.start_session()
                raced.append(fake.runner.pid())
            return real(shape, **kw)
        before = len(fake.runner.calls)
        with mock.patch.object(cm, "start_supervisor", side_effect=raced_then_started):
            rc, out, err = self.connect(Backend(), fake)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(fake.runner.pid(), raced[0], "the job that raced in is the one running, never restarted")
        self.assertNotIn("stopped:", out)
        self.assertFalse({fake.runner.verb(c) for c in fake.runner.calls[before:]} & {"stop", "restart", "bootout"})

    def test_a_holder_that_is_not_the_managers_job_is_not_taken_for_it(self):
        fake, real, stray = self.bridged(), cm.start_supervisor, []

        def stray_first(shape, **kw):
            if shape == LA and not stray:
                stray.append(hold(self.root, L.name))   # a launcher no manager started, outside the lock protocol
            return real(shape, **kw)
        with mock.patch.object(cm, "start_supervisor", side_effect=stray_first):
            rc, out, err = self.connect(Backend(), fake)
        try:
            self.assertNotEqual(rc, 0, out + err)
            self.assertIn(f"(pid {stray[0].pid}) is not the process {cm.job_name(LA)} runs as", err)
            self.assertIsNone(stray[0].poll(), "the stray is left as it is")
            self.assertNotIn("next:", out)
        finally:
            unhold(stray[0])

    def replace_root_now(self):
        os.rename(self.root, str(self.root) + ".parked")
        os.mkdir(self.root)

    def test_a_root_replaced_after_install_and_before_the_bridge_is_wired_unwinds_this_attempt(self):
        fake, real = self.bridged(), cm.install

        def installed_then_replaced(*a, **kw):
            rc = real(*a, **kw)
            self.assertIsNotNone(fake.runner.pid(), "install started the job")
            self.replace_root_now()
            return rc
        with mock.patch.object(cm, "install", side_effect=installed_then_replaced):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertNotIn("next:", out)
        self.assertIn(f"{self.root} was replaced after install started {cm.job_name(LA)}; {cm.job_name(LA)}, which "
                      f"held the directory that was there, was stopped; nothing at {self.root} was touched", err)
        self.assertIsNone(fake.runner.pid(), "the job this attempt started is not left on the parked root")
        self.assertFalse(cm.service_path(BA).exists(), "the bridge was not wired")
        self.assertEqual(task_io.lock_held(str(self.root)), "free", "the replacement is untouched")
        self.assertEqual(os.listdir(self.root), [], "nothing was written into the replacement")

    def test_a_root_replaced_while_the_bridge_is_wired_never_starts_it_and_unwinds_the_rest(self):
        fake, real = self.bridged(), cb.wire_bridge

        def wired_then_replaced(*a, **kw):
            got = real(*a, **kw)
            self.assertTrue(cm.service_path(BA).exists())
            self.replace_root_now()
            return got
        with mock.patch.object(cb, "wire_bridge", side_effect=wired_then_replaced):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertNotIn("next:", out)
        self.assertIn(f"the bridge unit this attempt wrote ({cm.service_path(BA)}) was removed", err)
        self.assertIn(f"{cm.job_name(LA)}, which held the directory that was there, was stopped; nothing at {self.root} was "
                      "touched", err)
        self.assertFalse(cm.service_path(BA).exists(), "the bridge waited for the session, so it never started")
        self.assertFalse(fake.active)
        self.assertFalse({"stop", "bootout", "restart", "start"} & bridge_verbs(fake), "nothing named the bridge to its manager")
        self.assertIsNone(fake.runner.pid(), "this attempt's job is stopped")
        self.assertEqual(task_io.lock_held(str(self.root)), "free", "the replacement is untouched")
        self.assertEqual(os.listdir(self.root), [], "nothing was written into the replacement")

    def test_a_connect_whose_session_start_is_refused_leaves_no_bridge_running_without_it(self):
        """connect wrote the job and asked the manager to start it (its first change), the start was refused, and a
        bridge from before still runs: no session serves it, so the failed connect stops it too."""
        fake = self.bridged({("systemctl", "start"): 1})
        fake.active = fake.registered = True
        rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertFalse(fake.active, err)
        self.assertIn("since its session is not running", err)

    def test_a_bridge_left_unregistered_by_a_refused_start_is_the_only_bridge_removed(self):
        fake = self.bridged()
        fake.table["bootstrap"] = 5
        with mock.patch.object(cm, "is_macos", return_value=True):
            rc, out, err = self.connect(Backend(), fake)
            plist = cm.supervisor_path(BA)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"the bridge unit this attempt wrote ({plist}) was removed", err)
        self.assertFalse(plist.exists(), "an unregistered unit holding this attempt's bytes is removed")

    def test_connect_changes_no_jobs_definition_and_refuses_when_it_would(self):
        fake = self.running_job()
        fake.runner.stop_session()
        conf = cli.config_path(LA)
        conf.write_text(conf.read_text().replace("ROOT=", "# edited by hand\nROOT=", 1))
        before = conf.read_bytes()
        rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"{conf} differs from what connect would write; connect changes no job's definition", err)
        self.assertEqual(conf.read_bytes(), before)
        self.assertIsNone(fake.runner.pid(), "nothing was started")

    def agent_lock_holder(self):
        """The production agent lock held by another process."""
        p = subprocess.Popen([sys.executable, "-c", AGENT_LOCK_HOLDER % (str(PKG), str(self.root))],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        self.addCleanup(lambda: unhold(p) if p.poll() is None else None)
        return p

    def test_both_adapters_resolve_one_lock_for_one_agent(self):
        from ag2_sparrow.adapters.codex import connect_cli as other
        self.assertIs(other.cm.agent_lock_path, cm.agent_lock_path, "one production lock owner")
        self.assertIs(other.cb.agent_root, cb.agent_root)
        root = cb.agent_root(AGENT)[0]
        self.assertEqual(root, str(self.root))
        lock = cm.agent_lock_path(root)
        self.assertEqual(lock, _dirs.agents_dir() / ".locks" / f"{self.root.name}.op")
        self.assertFalse(str(lock).startswith(str(self.root) + os.sep), "outside the root")
        self.root.mkdir(parents=True)
        os.rename(self.root, str(self.root) + ".parked")
        os.mkdir(self.root)
        self.assertEqual(cm.agent_lock_path(str(self.root)), lock, "a replacement at the path takes the same lock")

    def test_install_uninstall_and_wiring_wait_for_the_agent_lock_and_refuse(self):
        fake = self.running_job()
        fake.runner.stop_session()
        cfg = cm.read_config(cli.config_path(LA))
        holder = self.agent_lock_holder()
        with mock.patch.object(cm, "AGENT_LOCK_WAIT_S", 0.3), mock.patch.object(sm, "_run", side_effect=fake), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            before = len(fake.runner.calls)
            self.assertEqual(cm.install(L, {}, str(self.root)), 1)
            self.assertEqual(cm.uninstall(LA), 1)
            wired = cb.wire_bridge_bound(cfg, cli.COMMAND, BA, L, None)
        self.assertEqual(err.getvalue().count("another operation on this agent is in progress"), 2, err.getvalue())
        self.assertIn("another operation on this agent is in progress", wired)
        self.assertEqual(fake.runner.calls[before:], [], "nothing reached the manager")
        self.assertTrue(cli.config_path(LA).exists(), "uninstall removed nothing")
        holder.kill()
        holder.wait(timeout=30)
        with cm.agent_lock(str(self.root), wait_s=1):
            pass                                    # a dead holder's flock is gone: nothing stale remains

    def test_a_launcher_does_not_act_on_the_root_until_the_operation_ends(self):
        self.root.mkdir(parents=True)
        holder = self.agent_lock_holder()
        rc, err, _seen = self.launch_with(LA)
        self.assertEqual(rc, cm.EXIT_RETRY, err)
        self.assertIn("the launcher does not act on the root, and the manager starts it again", err)
        unit = cm.render_supervisor(LA)[0]
        self.assertIn(f"RestartPreventExitStatus={cm.EXIT_HELD}\n", unit, "only 75 stops systemd's restarts")
        self.assertNotIn(str(cm.EXIT_RETRY), unit.split("RestartPreventExitStatus=")[1].splitlines()[0])
        self.assertNotEqual(cm.launchd_exit(cm.EXIT_RETRY), 0, "launchd restarts a non-zero exit")
        unhold(holder)
        rc, err, _seen = self.launch_with(LA)
        self.assertEqual(rc, cm.EXIT_UNUSABLE, err)
        self.assertIn("stop here", err, "once the operation ended the launcher took the root")

    def test_the_other_adapter_and_a_launcher_wait_while_connect_holds_the_agent(self):
        real, seen = cm.install, []

        def contended(*a, **kw):
            r = subprocess.run([sys.executable, "-c", CONTENDER % (str(PKG), "codex", "connect_cli", str(self.root),
                                                                    str(self.root))],
                               capture_output=True, text=True, timeout=60)
            seen.append(r.stdout + r.stderr)
            return real(*a, **kw)
        with mock.patch.object(cm, "install", side_effect=contended):
            rc, out, err = self.connect(Backend())
        self.assertEqual(rc, 0, out + err)
        self.assertIn("install 1", seen[0])
        self.assertIn("another operation on this agent is in progress", seen[0])
        self.assertIn("launch an operation on this agent held", seen[0])

    def test_unwinding_leaves_a_unit_that_is_no_longer_this_attempts(self):
        fake, real = self.bridged(), cb.wire_bridge
        foreign = "[Service]\nExecStart=/a-successor\n# " + L.marker + "\n"

        def wired_then_replaced(*a, **kw):
            got = real(*a, **kw)
            self.replace_root_now()
            cm.supervisor_path(LA).write_text(foreign)     # a successor's definition, published meanwhile
            return got
        with mock.patch.object(cb, "wire_bridge", side_effect=wired_then_replaced):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertNotIn("next:", out)
        self.assertIn(f"{cm.job_name(LA)} is no longer the job this connect started (its unit or launcher changed), so "
                      f"it was left as it is: stop it by hand", err)
        self.assertEqual(cm.supervisor_path(LA).read_text(), foreign, "the successor's unit is untouched")
        self.assertIsNotNone(fake.runner.pid(), "its job was not stopped")

    def test_unwinding_stops_this_attempts_job_and_leaves_a_held_replacement_alone(self):
        fake, real, other = self.bridged(), cb.wire_bridge, []

        def wired_then_replaced(*a, **kw):
            got = real(*a, **kw)
            self.replace_root_now()
            other.append(hold(self.root, "codex-local"))
            return got
        with mock.patch.object(cb, "wire_bridge", side_effect=wired_then_replaced):
            rc, out, err = self.connect(Backend(), fake)
        try:
            self.assertNotEqual(rc, 0, out + err)
            self.assertIsNone(fake.runner.pid(), "this attempt's job was stopped")
            self.assertIsNone(other[0].poll(), "the replacement's holder is alive")
            rec = task_io.lock_holder(str(self.root))
            self.assertEqual((rec["adapter"], rec["pid"]), ("codex-local", other[0].pid), "and still holds it")
        finally:
            unhold(other[0])

    def test_a_definition_published_by_another_writer_first_is_not_clobbered(self):
        fake, real, theirs = self.bridged(), cm.tempfile.mkstemp, "[Service]\nExecStart=/theirs\n"
        sup = cm.supervisor_path(LA)

        def raced(*a, **kw):
            if kw.get("prefix") == f".{sup.name}." and not sup.exists():
                sup.parent.mkdir(parents=True, exist_ok=True)
                sup.write_text(theirs)                   # the other writer publishes between check and write
            return real(*a, **kw)
        with mock.patch.object(cm.tempfile, "mkstemp", side_effect=raced):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"{sup} appeared with other bytes than connect's", err)
        self.assertEqual(sup.read_text(), theirs, "the other writer's bytes are kept")
        self.assertIsNone(fake.runner.pid(), "nothing was started")

    def test_a_loaded_launchd_job_counts_as_started_only_for_connects_start(self):
        loaded = (5, "", "Bootstrap failed: 5: Input/output error\n")
        table = {("launchctl", "bootstrap"): loaded, ("launchctl", "print"): (0, "\tstate = running\n", "")}
        run = lambda cmd, timeout=30: subprocess.CompletedProcess(cmd, *table[(cmd[0], cmd[1])])  # noqa: E731
        with mock.patch.object(cm, "is_macos", return_value=True), mock.patch.object(sm, "_run", side_effect=run):
            self.assertEqual(cm.start_supervisor(LA, restart=False), "")
            self.assertIn("launchctl bootstrap failed", cm.start_supervisor(LA))

    def _main_with(self, fake, *argv):
        with mock.patch.object(sm, "_run", side_effect=fake):
            return self.main(*argv)

    def launch_with(self, shape, gap=None):
        """The production launcher sequence on the root, `gap()` run just before it takes the root."""
        real, seen = task_io.acquire_lock, []

        def acquire(*a, **kw):
            if gap is not None:
                seen.append(gap())
            return real(*a, **kw)
        err = io.StringIO()
        with mock.patch.object(cm, "LAUNCH_WAIT_S", 0.3), mock.patch.object(task_io, "acquire_lock", side_effect=acquire), \
                contextlib.redirect_stderr(err):
            rc = cm.hold_and_supervise(shape, str(self.root), {"sig": None}, lambda owner, root: (None, "stop here"),
                                       None, None, None, "run", "test")
        return rc, err.getvalue(), seen

    def other_thread_takes_the_agent_lock(self):
        got = []

        def take():
            try:
                with cm.agent_lock(str(self.root), wait_s=0.3):
                    got.append("taken")
            except cm.Busy:
                got.append("busy")
        t = threading.Thread(target=take)
        t.start()
        t.join(10)
        return got[0]

    def test_launcher_keeps_the_agent_lock_from_admission_until_it_holds_the_root(self):
        self.root.mkdir(parents=True)
        rc, err, seen = self.launch_with(L, gap=self.other_thread_takes_the_agent_lock)
        self.assertEqual(rc, cm.EXIT_UNUSABLE, err)
        self.assertIn("stop here", err, "the launcher took the root")
        self.assertEqual(seen, ["busy"], "no actor gets the agent lock between admission and the root")

    def test_install_holds_one_agent_lock_across_its_setup_and_its_bridge(self):
        fake = self.bridged()
        seed_seat(cli.bridge_home(self.root), BEARER)
        real, gap = cm.install, []

        def installed_then_uninstall_tries(*a, **kw):
            rc = real(*a, **kw)
            got = []
            t = threading.Thread(target=lambda: got.append(cm.uninstall(LA)))
            with mock.patch.object(cm, "AGENT_LOCK_WAIT_S", 0.3), mock.patch.object(cm, "FENCE_WAIT_S", 0.3), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                t.start()
                t.join(30)
            gap.append((got[0], err.getvalue()))
            return rc
        with mock.patch.object(cm, "install", side_effect=installed_then_uninstall_tries):
            rc, out, err = (lambda fake: self.install_default(fake, "--token-env-file", str(cli.bridge_home(self.root) / "channels" / "ag2space" / ".env")))(fake)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(gap[0][0], 1, "uninstall could not step in between install's setup and its bridge")
        self.assertIn("another operation on this agent is in progress", gap[0][1])
        self.assertTrue(cli.config_path(LA).exists() and cm.service_path(BA).exists(), "installed and wired whole")

    def test_uninstall_rereads_the_root_under_its_lock_and_never_acts_on_an_unlocked_one(self):
        a, b, c = self.home / "agents" / "agent-a", self.home / "agents" / "agent-b", self.home / "agents" / "agent-c"
        for swaps, acted in (([b], True), ([b, c], False)):
            with self.subTest(swaps=len(swaps)):
                self.configure(LA, ROOT=str(a))
                real_lock, real_un, todo, ran = cm.agent_lock, cm._uninstall_locked, list(swaps), []

                def lock(root, *args, **kw):
                    got = real_lock(root, *args, **kw)
                    if todo:
                        self.configure(LA, ROOT=str(todo.pop(0)))   # another install moves the root meanwhile
                    return got

                def uninstall_locked(*args, **kw):
                    root = cm.read_config(cli.config_path(LA))["ROOT"]
                    ran.append((root, any(str(cm.agent_lock_path(root)) in str(k) for k in cm._AGENT_HELD)))
                    return 0
                err = io.StringIO()
                with mock.patch.object(cm, "agent_lock", side_effect=lock), \
                        mock.patch.object(cm, "_uninstall_locked", side_effect=uninstall_locked), \
                        contextlib.redirect_stderr(err):
                    rc = cm.uninstall(LA)
                if acted:
                    self.assertEqual((rc, ran), (0, [(str(b), True)]), "acted on B with B's lock held")
                else:
                    self.assertEqual((rc, ran), (1, []), "fail closed: nothing acted on")
                    self.assertIn("configured root changed while uninstall waited for its lock", err.getvalue())

    def test_uninstall_with_no_config_waits_out_any_agent_operation(self):
        self.root.mkdir(parents=True)
        holder = self.agent_lock_holder()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cm.uninstall(LA), 1)
        # An agent's own job names its agent's root by its key, so it waits on that root's own lock.
        self.assertIn(f"another operation on this agent is in progress (lock {cm.agent_lock_path(self.root)})",
                      err.getvalue())
        unhold(holder)
        with contextlib.redirect_stdout(io.StringIO()) as out, mock.patch.object(sm, "_run", side_effect=self.bridged()):
            self.assertEqual(cm.uninstall(LA), 0)
        self.assertIn("nothing to remove", out.getvalue())

    def test_an_identity_mismatch_at_start_leaves_a_unit_that_is_no_longer_this_attempts(self):
        fake, real = self.bridged(), cm._fresh_holder
        foreign = "[Service]\nExecStart=/a-successor\n"

        def held_then_moved(*a, **kw):
            rec = real(*a, **kw)
            if rec is not None and not os.path.exists(str(self.root) + ".parked"):
                self.replace_root_now()
                cm.supervisor_path(LA).write_text(foreign)
            return rec
        with mock.patch.object(cm, "_fresh_holder", side_effect=held_then_moved):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn("is no longer the job this connect started (its unit or launcher changed)", err)
        self.assertIsNotNone(fake.runner.pid(), "not stopped")
        self.assertEqual(cm.supervisor_path(LA).read_text(), foreign)

    def test_the_real_launcher_started_by_install_and_by_connect_ends_up_alive(self):
        for how in ("install", "connect"):
            with self.subTest(how):
                fake = self.bridged()
                fake.runner.real = True                     # hold_and_supervise, through launch_admission
                if how == "install":
                    seed_seat(cli.bridge_home(self.root), BEARER)
                    rc, out, err = (lambda fake: self.install_default(fake, "--token-env-file", str(cli.bridge_home(self.root) / "channels" / "ag2space" / ".env")))(fake)
                else:
                    rc, out, err = self.connect(Backend(), fake)
                self.assertEqual(rc, 0, out + err)
                self.assertIsNotNone(fake.runner.session)
                self.assertIsNone(fake.runner.session.poll(), "the launcher is alive")
                rec = task_io.lock_holder(str(self.root))
                self.assertEqual((rec["owner_pid"], rec["live"]), (fake.runner.pid(), True), "and holds the root")
                fake.runner.stop_session()
                with mock.patch.object(sm, "_run", side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cm.uninstall(LA), 0)

    def test_a_launcher_holding_the_agent_lock_past_the_wait_is_left_and_reported(self):
        for shim in (False, True):
            with self.subTest("launchd shim child" if shim else "systemd direct"):
                fake = self.bridged()
                r = fake.runner
                r.real, r.shim = True, shim
                r.pause, r.acted = str(self.home / f"resume-{shim}"), str(self.home / f"acted-{shim}")
                with mock.patch.object(cm, "AGENT_LOCK_WAIT_S", 0.5), mock.patch.object(cm, "is_macos", return_value=shim), \
                        mock.patch.object(cm, "_signal_exact", wraps=cm._signal_exact) as signalled:
                    rc, out, err = self.connect(Backend(), fake)
                    by_hand = f"({cm.stop_command(LA)})"
                verbs, job = [r.verb(c) for c in r.calls], r.pid()
                Path(r.pause).touch()
                try:
                    self.assertNotEqual(rc, 0, out + err)
                    self.assertNotIn("next:", out)
                    self.assertIn("did not acquire", err)
                    self.assertIn("so nothing was stopped", err)
                    self.assertIn(by_hand, err, "what is left comes with the command that stops it")
                    self.assertFalse({"stop", "bootout", "disable"} & set(verbs), "no cleanup without the agent lock")
                    signalled.assert_not_called()
                    self.assertIsNotNone(job, "the job is left, as reported")
                finally:
                    for launcher in r.spawned:              # cleanup, so one shape cannot leak into the next
                        if launcher.poll() is None:
                            os.killpg(launcher.pid, signal.SIGKILL)
                            launcher.wait(timeout=30)
                    r.session = None

    def test_the_real_launcher_through_launchds_shim_ends_up_alive(self):
        fake = self.bridged()
        fake.runner.real, fake.runner.shim = True, True
        with mock.patch.object(cm, "is_macos", return_value=True):
            rc, out, err = self.connect(Backend(), fake)
            self.assertEqual(rc, 0, out + err)
            rec = task_io.lock_holder(str(self.root))
            self.assertTrue(rec["live"])
            self.assertEqual(cm._parent_pid(rec["owner_pid"]), fake.runner.pid(), "the shim's child holds the root")
            self.assertEqual({p.suffix for p in cm.agent_lock_path(str(self.root)).parent.iterdir()}, {".op"},
                             "no start files: the operation hands the launcher nothing but the lock")
            fake.runner.stop_session()

    def test_a_stop_reaches_a_launcher_waiting_in_admission_at_once(self):
        for shim in (False, True):
            with self.subTest("launchd shim child" if shim else "systemd direct"):
                fake = self.bridged()
                fake.runner.real, fake.runner.shim = True, shim
                fake.runner.acted = str(self.home / f"acted-{shim}")
                ready = Path(fake.runner.acted + ".ready")

                def refused_once_waiting(*a, **kw):
                    deadline = time.monotonic() + 30
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)                # the launcher is in admission, waiting for this lock
                    started.append(time.monotonic())
                    return "the bridge was refused"
                started = []
                with mock.patch.object(cm, "is_macos", return_value=shim), \
                        mock.patch.object(cb, "wire_bridge", side_effect=refused_once_waiting):
                    rc, out, err = self.connect(Backend(), fake)
                took = time.monotonic() - started[0]
                self.assertNotEqual(rc, 0, out + err)
                self.assertIn("this attempt was undone", err)
                self.assertLess(took, 8, "the launcher left admission on the stop, not at its 20 s wait or the kill")
                self.assertTrue(all(launcher.poll() is not None for launcher in fake.runner.spawned))
                self.assertFalse(Path(fake.runner.acted).exists(), "the stopped launcher never touched the root")

    def test_a_busy_lock_held_by_another_operation_leaves_the_job_to_it(self):
        fake, real, other = self.bridged(), cm._fresh_holder, []

        def other_operation_first(*a, **kw):
            if not other:
                other.append(self.agent_lock_holder())  # another operation takes the agent lock and keeps it
            return real(*a, **kw)
        fake.runner.root = None                         # the manager accepts the start; no launcher comes
        before = len(fake.runner.calls)
        with mock.patch.object(cm, "AGENT_LOCK_WAIT_S", 0.5), \
                mock.patch.object(cm, "_fresh_holder", side_effect=other_operation_first):
            rc, out, err = self.connect(Backend(), fake)
        verbs = [fake.runner.verb(c) for c in fake.runner.calls[before:]]
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"another operation holds the agent lock (operation pid {other[0].pid})", err)
        self.assertFalse({"stop", "bootout", "disable"} & set(verbs), "nothing was stopped under another operation")

    def test_unwinding_leaves_a_job_the_manager_restarted_meanwhile(self):
        fake, real, restarted = self.bridged(), cb.wire_bridge, []

        def wired_then_restarted(*a, **kw):
            got = real(*a, **kw)
            self.replace_root_now()
            fake.runner.start_session()                  # someone restarted the unit: same bytes, another job
            restarted.append(fake.runner.pid())
            return got
        with mock.patch.object(cb, "wire_bridge", side_effect=wired_then_restarted):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn("is no longer the job this connect started (its unit or launcher changed)", err)
        self.assertEqual(fake.runner.pid(), restarted[0], "the restarted job was not stopped")

    def test_an_agent_lock_directory_others_can_use_is_refused_cleanly(self):
        self.root.mkdir(parents=True)
        locks = cm.agent_lock_path(str(self.root)).parent
        locks.mkdir(parents=True, exist_ok=True)
        locks.chmod(0o777)
        self.addCleanup(locks.chmod, 0o700)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cm.install(L, {}, str(self.root)), 1)
        self.assertIn("cannot be used", err.getvalue())
        self.assertIn("is not a directory only this user can use", err.getvalue())
        rc, err, _seen = self.launch_with(LA)
        self.assertEqual(rc, cm.EXIT_RETRY, err)
        self.assertIn("cannot be used", err)
        self.assertNotIn("Traceback", err)

    def test_a_start_another_actor_won_is_never_stopped_by_this_attempts_undo(self):
        for shim in (False, True):
            with self.subTest("launchd shim child" if shim else "systemd direct"):
                fake, real, external = self.bridged(), cm._start_pending, []
                fake.runner.real, fake.runner.shim = True, shim

                def external_first(shape, *a, **kw):
                    if shape == LA and not external:
                        fake.runner.start_session(by_manager=True)   # someone else starts the job after the check
                        external.append(fake.runner.session)
                    return real(shape, *a, **kw)
                with mock.patch.object(cm, "is_macos", return_value=shim), \
                        mock.patch.object(cm, "_start_pending", side_effect=external_first), \
                        mock.patch.object(cb, "wire_bridge", return_value="the bridge was refused"):
                    rc, out, err = self.connect(Backend(), fake)
                try:
                    self.assertNotEqual(rc, 0, out + err)
                    self.assertIn("was already running when this operation asked for it, so it is not this operation's",
                                  err)
                    self.assertIsNone(external[0].poll(), "the external generation is alive")
                    self.assertIs(fake.runner.session, external[0], "and still the manager's job")
                finally:
                    fake.runner.stop_session()

    def test_an_exception_after_the_start_is_undone_under_the_lock(self):
        for how in ("connect", "install"):
            for shim in (False, True):
                with self.subTest(how=how, shape="launchd shim child" if shim else "systemd direct"):
                    fake = self.bridged()
                    fake.runner.real, fake.runner.shim = True, shim
                    fake.runner.acted = str(self.home / f"acted-{how}-{shim}")
                    seed_seat(cli.bridge_home(self.root), BEARER)
                    with mock.patch.object(cm, "is_macos", return_value=shim), \
                            mock.patch.object(cb, "wire_bridge", side_effect=OSError("bridge write failed")), \
                            self.assertRaises(OSError):
                        if how == "connect":
                            self.connect(Backend(), fake)
                        else:
                            self.install_default(fake, "--token-env-file", str(cli.bridge_home(self.root) / "channels" / "ag2space" / ".env"))
                    for launcher in fake.runner.spawned:
                        launcher.wait(timeout=30)
                    self.assertEqual(cm._PENDING.get(threading.get_ident(), []), [], "nothing left pending")
                    self.assertFalse(cm.agent_lock_held(str(self.root)))
                    self.assertIsNone(fake.runner.pid(), "the job this attempt started was stopped")
                    self.assertFalse(Path(fake.runner.acted).exists(), "and never touched the root")

    def test_a_running_job_refuses_connect_before_the_code_is_spent_and_is_left_running(self):
        fake = self.running_job()
        pid, before, backend = fake.runner.pid(), (self.seat() / "token").read_bytes(), Backend()
        rc, out, err = self.connect(backend, fake)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertIn(f"{cm.job_name(LA)} is running for {self.root}; stop it first (systemctl --user stop "
                      f"{LA.service_name}), then connect again; the code was not used", err)
        self.assertEqual(backend.calls, [], "the code is not spent")
        self.assertNotIn("stopped:", out)
        self.assertEqual((self.seat() / "token").read_bytes(), before)
        self.assert_job_runs(fake, pid)

    def test_an_unreadable_job_state_refuses_connect_before_the_code_is_spent(self):
        backend = Backend()
        with mock.patch.object(cm, "manager_state", return_value="unknown"):
            rc, out, err = self.connect(backend)
        self.assertEqual(rc, cli.EXIT_UNUSABLE, out + err)
        self.assertIn(f"state cannot be read; connect writes the seat only while it is stopped: stop it "
                      f"(systemctl --user stop {LA.service_name}), then connect again; the code was not used", err)
        self.assertEqual(backend.calls, [])

    def test_a_stopped_job_connects_and_install_starts_it_with_the_new_seat(self):
        fake = self.running_job()
        fake.runner.stop_session()
        rotated = Backend(answer={"relay_url": RELAY, "bearer": "rotated-bearer-OOOO5555", "agent_id": AGENT})
        rc, out, err = self.connect(rotated, fake)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"connected: {AGENT}", out)
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|rotated-bearer-OOOO5555\n")
        self.assert_job_runs(fake)

    def test_a_root_replaced_before_install_gets_nothing_installed_or_started(self):
        fake, real = self.bridged(), cm.install

        def replaced_then_installed(*a, **kw):
            os.rename(self.root, str(self.root) + ".parked")
            os.mkdir(self.root)
            return real(*a, **kw)
        with mock.patch.object(cm, "install", side_effect=replaced_then_installed):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"{self.root} is no longer the directory connect wrote the seat for (it was replaced); nothing "
                      "was applied", err)
        self.assertIsNone(fake.runner.pid(), "nothing was started on the replacement")
        self.assertEqual(task_io.lock_held(str(self.root)), "free")

    def test_a_root_replaced_while_install_starts_the_job_gets_that_job_stopped(self):
        fake, real = self.bridged(), cm.start_supervisor

        def replaced_then_started(shape, **kw):
            if shape == LA:
                os.rename(self.root, str(self.root) + ".parked")
                os.mkdir(self.root)
            return real(shape, **kw)
        with mock.patch.object(cm, "start_supervisor", side_effect=replaced_then_started):
            rc, out, err = self.connect(Backend(), fake)
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(f"{self.root} was replaced after install started {cm.job_name(LA)}", err)
        self.assertIn(f"{cm.job_name(LA)}, which held the directory that was there, was stopped", err)
        self.assertNotIn("next:", out)
        self.assertIsNone(fake.runner.pid(), "no launcher is left to take the replacement")
        self.assertEqual(task_io.lock_held(str(self.root)), "free")

    def test_without_plugin_dir_the_shipped_plugin_is_used(self):
        self.assertTrue((cli.PLUGIN / ".claude-plugin" / "plugin.json").is_file())
        rc, _out, err = self.connect(Backend(), plugin=False)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cfg()["PLUGIN_DIR"], str(cli.PLUGIN))

    def test_the_instance_is_minted_once_and_kept_across_a_reconnect(self):
        fake = self.bridged()
        self.assertEqual(self.connect(Backend(), fake)[0], 0)
        first = (self.seat() / ".env").read_text()
        fake.runner.stop_session()          # a rotation is stop, then connect
        rotated = Backend(answer={"relay_url": RELAY, "bearer": "rotated-bearer-OOOO5555", "agent_id": AGENT})
        rc, _out, err = self.connect(rotated, fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.seat() / ".env").read_text(), first, "same instance, same seat")
        self.assertEqual((self.seat() / "token").read_text(), f"{RELAY}|rotated-bearer-OOOO5555\n")

    def test_a_used_or_expired_code_says_make_a_new_one_and_writes_nothing(self):
        rc, _out, err = self.connect(Backend(404))
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertIn("already used or has expired; make a new one in s.space", err)
        self.assertFalse(self.seat().exists())
        self.assertFalse(cli.config_path(LA).exists())

    def test_a_503_is_retried_with_backoff_then_redeemed(self):
        sleeps = []
        backend = Backend(503, 503)
        rc, _out, err = self.connect(backend, sleeps=sleeps)
        self.assertEqual(rc, 0, err)
        self.assertEqual((len(backend.calls), sleeps), (3, [1, 2]))

    def test_a_backend_that_stays_503_gives_up_and_the_code_is_still_valid(self):
        sleeps = []
        backend = Backend(*[503] * 10)
        rc, _out, err = self.connect(backend, sleeps=sleeps)
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertEqual((len(backend.calls), sleeps), (len(cm.REDEEM_BACKOFF_S) + 1, list(cm.REDEEM_BACKOFF_S)))
        self.assertIn("the code is still valid", err)
        self.assertFalse(self.seat().exists())

    def test_a_code_on_the_command_line_still_works_but_warns(self):
        rc, _out, err = self.connect(Backend(), None, "--code", CODE)
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: a code on the command line is kept by ps and shell history", err)

    def test_no_code_prompts_on_a_terminal_and_is_refused_without_one(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        backend = Backend()
        rc, _out, err = self.connect(backend, stdin=io.StringIO(""), code_args=())
        self.assert_code_unspent(backend, rc, err)
        self.assertIn("pass --code - and pipe the code in", err)
        backend = Backend()
        rc, _out, err = self.connect(backend, stdin=io.StringIO(""))
        self.assert_code_unspent(backend, rc, err)
        self.assertIn("not an agent code", err, "an empty stdin is no code")
        with mock.patch.object(cm.getpass, "getpass", return_value=CODE) as asked:
            rc, _out, err = self.connect(Backend(), stdin=Tty(""), code_args=())
        self.assertEqual((rc, asked.call_count), (0, 1), err)

    def test_a_plain_http_base_is_refused_unless_it_is_loopback(self):
        backend = Backend()
        rc, _out, err = self.connect(backend, base="http://backend.example/api")
        self.assert_code_unspent(backend, rc, err)
        self.assertIn("--base is not https", err)
        loop = Backend(answer={"relay_url": "http://127.0.0.1:9/relay", "bearer": BEARER, "agent_id": AGENT})
        rc, _out, err = self.connect(loop, base="http://127.0.0.1:9/api")
        self.assertEqual(rc, 0, err)

    def test_a_relay_url_that_is_plain_http_or_off_domain_is_never_written(self):
        for relay, extra, why in (("http://relay.example/relay", (), "is not https"),
                                  ("https://elsewhere.test/relay", (), "--trust-relay-host elsewhere.test")):
            with self.subTest(relay=relay):
                rc, _out, err = self.connect(Backend(answer={"relay_url": relay, "bearer": BEARER, "agent_id": AGENT}))
                self.assertEqual(rc, cli.EXIT_UNUSABLE)
                self.assertIn(why, err)
                self.assertIn("the code is spent but the seat was not written", err)
                self.assertFalse(self.seat().exists())
                self.assertNotIn(BEARER, err)
        rc, _out, err = self.connect(Backend(answer={"relay_url": "https://elsewhere.test/relay", "bearer": BEARER,
                                                     "agent_id": AGENT}), None, "--trust-relay-host", "elsewhere.test")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.seat() / "token").read_text(), f"https://elsewhere.test/relay|{BEARER}\n")

    def test_a_202_still_redeeming_is_retried_honouring_retry_after(self):
        sleeps = []
        backend = Backend(*[(202, {"state": "redeeming", "retry_after": 999})] * 10)
        rc, _out, err = self.connect(backend, sleeps=sleeps)
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertEqual(sleeps, [cm.RETRY_AFTER_MAX_S] * len(cm.REDEEM_BACKOFF_S), "capped")
        self.assertIn("still redeeming the code; run connect again with the same code", err)
        sleeps = []
        backend = Backend((202, {"state": "redeeming", "retry_after": 3}), (202, {"state": "redeeming"}))
        rc, _out, err = self.connect(backend, sleeps=sleeps)
        self.assertEqual(rc, 0, err)
        self.assertEqual((len(backend.calls), sleeps), (3, [3.0, 2]), "retry_after, then the backoff")

    def test_a_404_names_why_the_code_is_gone(self):
        for body, why in (({"error": "gone"}, "already used or has expired; make a new one"),
                          ({"error": "agent_revoked"}, "the agent was removed in s.space; add it again"),
                          ({}, "already used or has expired")):
            with self.subTest(body=body):
                rc, _out, err = self.connect(Backend((404, body)))
                self.assertEqual(rc, cli.EXIT_UNUSABLE)
                self.assertIn(why, err)

    def assert_code_unspent(self, backend, rc, err):
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertEqual(backend.calls, [], "the backend is never asked")
        self.assertIn("the code was not used", err)
        self.assertFalse(self.seat().exists())

    def test_a_logged_out_claude_never_spends_the_code(self):
        (self.home / "claude-auth").write_text('{"loggedIn": false}')
        backend = Backend()
        rc, _out, err = self.connect(backend)
        self.assert_code_unspent(backend, rc, err)
        self.assertIn("not logged in", err)

    def test_a_missing_tmux_or_plugin_never_spends_the_code(self):
        with mock.patch.object(launcher, "tmux_version", return_value=None):
            backend = Backend()
            rc, _out, err = self.connect(backend)
            self.assert_code_unspent(backend, rc, err)
        (self.plugin / ".claude-plugin" / "plugin.json").unlink()
        backend = Backend()
        rc, _out, err = self.connect(backend)
        self.assert_code_unspent(backend, rc, err)
        self.assertIn("not a Claude Code plugin", err)

    def off_path_tmux(self, version="3.4"):
        """tmux only in the desktop app's engine/bin: a link (under a path with spaces) to a fake that logs
        each argv; PATH holds no tmux and the package-manager dirs are out of reach."""
        store = self.home / "tmux store" / ("tmux-" + version)
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s.log"\necho "tmux %s"\n' % (store, version))
        store.chmod(0o755)
        link = self.home.joinpath(*launcher.APP_TMUX_DIR, "tmux")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(store)
        for p in (mock.patch.dict(os.environ, {"PATH": str(self.bin)}), mock.patch.object(launcher, "TMUX_DIRS", ()),
                  mock.patch.object(launcher, "tmux_version", REAL_TMUX_VERSION)):
            p.start()
            self.addCleanup(p.stop)
        return link, Path(str(store) + ".log")

    def test_tmux_off_path_is_resolved_recorded_and_used_by_its_absolute_path_through_a_restart(self):
        link, log = self.off_path_tmux()
        rc, out, err = self.connect(Backend())
        self.assertEqual(rc, 0, out + err)
        self.assertIn(" ", str(link))
        self.assertEqual(self.cfg()["TMUX"], str(link), "the link as named, not its target")
        seen = []
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", cm.AGENT_VAR: LA.agent}), \
                mock.patch.object(launcher.Launcher, "_serve", lambda lc: seen.append(lc.tm.tmux) or 0):
            self.assertEqual(self.main("run")[0], 0, "the manager's restart, on launchd's PATH")
            self.assertEqual(launcher.Launcher(cli.load_config(LA)[0], cli.config_dir(LA)).tm.tmux, str(link),
                             "stop and uninstall use the same")
            execs = []
            with mock.patch.object(os, "execv", lambda f, argv: execs.append(f)):
                self.main("attach")
        self.assertEqual((seen, execs), ([str(link)], [str(link)]))
        self.assertEqual(log.read_text().splitlines().count("-u -V"), 3, "connect, install and the restart checked it")

    def test_a_missing_old_or_silent_tmux_refuses_before_the_code_is_read(self):
        def refused(why, *extra):
            backend, stdin = Backend(), io.StringIO(CODE + "\n")
            rc, _out, err = self.connect(backend, None, *extra, stdin=stdin)
            self.assertEqual(rc, cli.EXIT_UNUSABLE, err)
            self.assertIn(why, err)
            self.assertIn("the code was not used", err)
            self.assertEqual((backend.calls, stdin.read()), ([], CODE + "\n"), "no code read, none redeemed")
            self.assertFalse(cli.config_path(LA).exists())
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}), mock.patch.object(launcher, "TMUX_DIRS", ()):
            refused("tmux was not found on PATH or in ~/Library/Application Support/space.ag2.app/engine/bin")
            refused("--tmux /nowhere/tmux is not an executable file", "--tmux", "/nowhere/tmux")
        link, _log = self.off_path_tmux("3.1")
        refused(f"tmux 3.1 is older than the 3.2 this adapter needs (new-session -e): {link}")
        link.resolve().write_text("#!/bin/sh\nexec /bin/sleep 10\n")
        with mock.patch.object(cm, "RUN_TIMEOUT_S", 1):
            refused(f"{link} -V did not answer within 1 s")

    def reconnect_stopped(self):
        """The agent connected once, its job stopped: what a credential rotation starts from."""
        fake = self.running_job()
        fake.runner.stop_session()
        return fake, (self.seat() / "token").read_bytes()

    def assert_unspent(self, backend, rc, err, token, why):
        self.assertEqual(rc, cli.EXIT_UNUSABLE, err)
        self.assertIn(why, err)
        self.assertIn("the code was not used", err)
        self.assertEqual(backend.calls, [], "no redemption")
        self.assertEqual((self.seat() / "token").read_bytes(), token, "the seat is unchanged")

    def test_a_busy_agent_lock_refuses_a_reconnect_with_no_redemption(self):
        fake, token = self.reconnect_stopped()
        holder, backend = self.agent_lock_holder(), Backend()
        rc, _out, err = self.connect(backend, fake)
        unhold(holder)
        self.assert_unspent(backend, rc, err, token, f"another operation on an agent is in progress (lock "
                                                     f"{cm.agent_lock_path(self.root)})")

    def test_a_held_root_refuses_a_reconnect_with_no_redemption(self):
        fake, token = self.reconnect_stopped()
        holder, backend = hold(self.root, "codex-local"), Backend()
        try:
            rc, _out, err = self.connect(backend, fake)
        finally:
            unhold(holder)
        self.assert_unspent(backend, rc, err, token, f"{self.root} is held by launcher pid {holder.pid}")

    def test_a_seat_that_cannot_be_written_refuses_a_reconnect_with_no_redemption(self):
        fake, token = self.reconnect_stopped()
        os.rename(self.seat() / "token", self.home / "token.kept")
        os.mkfifo(self.seat() / "token")
        backend = Backend()
        rc, _out, err = self.connect(backend, fake)
        self.assertEqual((rc, backend.calls), (cli.EXIT_UNUSABLE, []), err)
        self.assertIn("cannot be written (", err)
        self.assertIn("the code was not used", err)

    def test_on_a_fresh_host_any_agent_operation_in_progress_refuses_with_no_redemption(self):
        holder, backend = self.agent_lock_holder(), Backend()
        rc, _out, err = self.connect(backend)
        unhold(holder)
        self.assertEqual((rc, backend.calls), (cli.EXIT_UNUSABLE, []), err)
        self.assertIn(f"another operation on an agent is in progress (lock {cm.agent_lock_path(self.root)})", err)
        self.assertFalse(cli.config_path(LA).exists())

    def test_under_a_host_supervisor_connect_declares_the_seat_with_no_vault_key(self):
        backend = Backend()
        fake = self.bridged()
        with mock.patch.object(_dirs, "bridge_host", return_value="sparrowd"):
            rc, out, err = self.connect(backend, fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(backend.calls), 1)
        self.assertFalse(cm.service_path(BA).exists(), "the supervisor runs it: no unit of its own")
        seat_env = str(self.seat() / ".env")
        cfg = self.cfg()
        self.assertEqual((cfg["BRIDGE"], cfg["TOKEN_ENV_FILE"], cfg["TOKEN_VAULT_KEY"]), ("sparrowd", seat_env, ""))
        decl = cli.bridge_home(self.root) / cli.DECLARATION
        body = json.loads(decl.read_text())
        self.assertEqual((body["token_vault_key"], body["env"]), ("", cli.launch_env(cfg)[0]))
        self.assertEqual((body["env"]["AG2_DEVICE_ENV"], body["env"]["REMOTE_TASK_TOKEN_FILE"], body["env"]["REMOTE_TASK_SEAT"]),
                         (seat_env, str(self.seat() / "token"), "1"))
        self.assertNotIn("vault set", out)
        self.assertNotIn(BEARER, decl.read_text() + out + err)

    def test_a_malformed_code_or_base_is_refused_before_anything_runs(self):
        for extra in (("--code", "web.code-1"), ("--base", "ftp://backend.example")):
            with self.subTest(extra=extra):
                backend = Backend()
                rc, _out, err = self.connect(backend, None, *extra)
                self.assertEqual((rc, backend.calls), (cli.EXIT_UNUSABLE, []))

    def test_the_bearer_is_never_in_argv_of_any_command_connect_runs(self):
        fake = self.bridged()
        seen = []
        real_run = subprocess.run

        def spy(cmd, *a, **kw):
            seen.append(list(cmd))
            return real_run(cmd, *a, **kw)
        with mock.patch.object(subprocess, "run", side_effect=spy):
            rc, _out, err = self.connect(Backend(), fake)
        self.assertEqual(rc, 0, err)
        for cmd in seen + fake.calls + [c for r in self.runners for c in r.calls]:
            self.assertNotIn(BEARER, " ".join(map(str, cmd)))


class RedeemEdges(unittest.TestCase):
    """connect_common.redeem_code's answers that never reach a seat."""

    def test_an_unreachable_backend_a_bad_answer_and_an_unreadable_error_body(self):
        def refused(req, timeout=None):
            raise urllib.error.URLError("connection refused")
        got, why = cm.redeem_code(BASE, CODE, opener=refused)
        self.assertEqual(got, None)
        self.assertIn("the code is still valid until it expires", why)
        for body in (b"[]", b"not json", json.dumps({"relay_url": RELAY, "agent_id": AGENT}).encode()):
            with self.subTest(body=body):
                got, why = cm.redeem_code(BASE, CODE, opener=lambda req, timeout=None, b=body: _Resp(200, b))
                self.assertEqual(got, None)
                self.assertIn("the code is spent, make a new one", why)

        class Unreadable(urllib.error.HTTPError):
            def read(self, *a):
                raise OSError("reset")

        def gone(req, timeout=None):
            raise Unreadable(req.full_url, 404, "x", {}, io.BytesIO(b""))
        self.assertIn("already used or has expired", cm.redeem_code(BASE, CODE, opener=gone)[1])
        teapot = lambda req, timeout=None: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, 418, "x", {}, io.BytesIO(b"")))
        self.assertEqual(cm.redeem_code(BASE, CODE, opener=teapot)[1], "s.space refused the code (HTTP 418)")

    def test_a_relay_must_be_on_the_bases_host_or_under_it_never_a_sibling_under_a_public_suffix(self):
        ok = (("https://api.example.co.uk/api", "https://api.example.co.uk/relay"),
              ("https://example.co.uk/api", "https://relay.example.co.uk/relay"),
              ("https://chat.ag2.space/api", "https://chat.ag2.space/relay"))
        for base, relay in ok:
            self.assertEqual(cm.relay_problem(base, relay), "", (base, relay))
        for base, relay in (("https://example.co.uk/api", "https://evil.co.uk/relay"),
                            ("https://api.example.co.uk/api", "https://relay.example.co.uk/relay"),
                            ("https://example.co.uk/api", "https://notexample.co.uk/relay")):
            with self.subTest(relay=relay):
                self.assertIn("--trust-relay-host", cm.relay_problem(base, relay))
        self.assertEqual(cm.relay_problem("https://example.co.uk/api", "https://evil.co.uk/relay", "evil.co.uk"), "")

    def test_retry_after_is_bounded_and_ignored_when_not_a_number(self):
        self.assertEqual(cm._retry_after({"retry_after": 0}, 4), 1.0)
        self.assertEqual(cm._retry_after({"retry_after": True}, 4), 4)
        self.assertEqual(cm._retry_after({"retry_after": "5"}, 4), 4)
        self.assertEqual(cm._retry_after({"retry_after": float("nan")}, 4), 4)


class ConnectAgentRoot(AgentRootBase):
    """With no explicit root the root is the agent's own, named by its id, and install wires the agent's
    bridge to exactly that root: its own unit here, a declaration under a host supervisor."""

    def test_the_default_root_is_the_agents_own_and_its_bridge_unit_feeds_exactly_it(self):
        fake = self.bridged()
        rc, out, err = self.install_default(fake)
        self.assertEqual(rc, 0, err)
        cfg = self.cfg()
        self.assertEqual((cfg["ROOT"], cfg["INBOX_DIR"], cfg["RESULTS_DIR"], cfg["AGENT_ID"], cfg["BRIDGE"]),
                         (str(self.root), str(self.root / "tasks"), str(self.root / "results"), AGENT, "unit"))
        home = cli.bridge_home(self.root)
        self.assertEqual(home, self.agents / "probe.agent_ag2.space.bridge", "beside the root, never in it")
        self.assertEqual((cfg["TOKEN_ENV_FILE"], cfg["HITL_STORE"]),
                         (str(self.relay), str(home / "state" / "hitl" / "requirements-claude-local")))
        unit = cm.service_path(BA).read_text()
        self.assertTrue(cm.marked(unit, BA.marker))
        self.assertIn(f"Written by {cli.COMMAND} install", unit)
        self.assertIn(f"ExecStart={self.bin / 'ag2-sparrow'}\n", unit, "the bridge's console script, no arguments")
        env = sorted(ln for ln in unit.splitlines() if ln.startswith('Environment="'))
        self.assertEqual(env, sorted([f'Environment="AGENT_CONNECT_TASK_DIR={self.root}/tasks"',
                                      f'Environment="AGENT_CONNECT_RESULT_DIR={self.root}/results"',
                                      f'Environment="AGENT_CONNECT_STATE_DIR={home}/state"',
                                      f'Environment="AGENT_MXID={AGENT}"', f'Environment="AG2_DEVICE_ENV={self.relay}"',
                                      # The install's key path: its own bridge signs every task the strict session checks.
                                      f'Environment="AG2_HMAC_KEY_FILE={self.home / "task-hmac.key"}"']))
        self.assertNotIn(TOKEN, unit + cli.config_path(LA).read_text(), "the token stays in its file")
        self.assertEqual(fake.verbs(), ["show", "enable", "restart"], "absent: registered and started, no stop")
        self.assertIn(f"started: {BA.service_name}, feeding {self.root}/tasks as {AGENT}", out)
        self.assertTrue((home / "state").is_dir())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["results", "tasks"], "the root stays fresh")

        fake.calls.clear()
        rc, out, err = self.install_default(fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.verbs(), ["show", "stop", "disable", "show", "enable", "restart"],
                         "a reinstall stops the old bridge, proves it stopped, then starts the new unit")

    def test_a_seat_token_env_file_runs_the_bridge_as_that_seat_alone(self):
        from ag2_sparrow import gateway_credential_sources as owner
        seat = self.home / "seat"
        bearer = "seat-bearer-LLLL2222"
        self.assertEqual(owner.write_seat_credentials(seat, agent_id=AGENT, instance="c" * 32, tier="team",
                                                      relay_url="https://gw.example/relay", bearer=bearer), "")
        seat_env = seat / "channels" / "ag2space" / ".env"
        rc, _out, err = self.install_default(self.bridged(), "--token-env-file", str(seat_env))
        self.assertEqual(rc, 0, err)
        unit = cm.service_path(BA).read_text()
        for line in (f'Environment="AG2_DEVICE_ENV={seat_env}"', 'Environment="REMOTE_TASK_SEAT=1"',
                     f'Environment="GATEWAY_INSTANCE={"c" * 32}"', 'Environment="REMOTE_TASK_TIER=team"',
                     f'Environment="REMOTE_TASK_TOKEN_FILE={seat_env.parent / "token"}"', f'Environment="AGENT_MXID={AGENT}"',
                     'Environment="REMOTE_TASK_TOKEN="', 'Environment="REMOTE_TASK_URL="', 'Environment="AGENT_ID="'):
            self.assertIn(line + "\n", unit)
        self.assertNotIn(bearer, unit + cli.config_path(LA).read_text(), "the bearer stays in the seat's token file")

    def test_a_seat_naming_another_agent_than_install_resolved_is_refused(self):
        from ag2_sparrow import gateway_credential_sources as owner
        seat = self.home / "seat"
        self.assertEqual(owner.write_seat_credentials(seat, agent_id="@other.agent:ag2.space", instance="d" * 32,
                                                      tier="owner", relay_url="https://gw.example/relay", bearer="b-1"), "")
        with mock.patch.dict(os.environ, {"AGENT_MXID": AGENT}):
            rc, _out, err = self.install_default(self.bridged(), "--token-env-file",
                                                 str(seat / "channels" / "ag2space" / ".env"))
        self.assertNotEqual(rc, 0)
        self.assertIn(f"names @other.agent:ag2.space, not {AGENT}", err)
        self.assertFalse(cm.service_path(BA).exists())

    def test_a_malformed_seat_env_file_is_refused_before_the_bridge_is_wired(self):
        seat_env = self.home / "seat" / ".env"
        seat_env.parent.mkdir(parents=True)
        seat_env.write_text(f"AGENT_MXID={AGENT}\nREMOTE_TASK_TOKEN_FILE={seat_env.parent / 'token'}\n")
        fake = self.bridged()
        rc, _out, err = self.install_default(fake, "--token-env-file", str(seat_env))
        self.assertNotEqual(rc, 0)
        self.assertIn("GATEWAY_INSTANCE", err)
        self.assertFalse(cm.service_path(BA).exists(), "no unit for a seat the bridge would refuse")

    def test_on_macos_the_bridge_is_a_plist_with_the_same_dirs(self):
        rc, _, err = self.install_default(self.bridged(), mac=True)
        self.assertEqual(rc, 0, err)
        body = plistlib.loads(cm.plist_path(BA).read_bytes())
        self.assertEqual(body["ProgramArguments"], [str(self.bin / "ag2-sparrow")])
        self.assertEqual((body["Label"], body["KeepAlive"]), (BA.agent_label, True))
        self.assertEqual({k: body["EnvironmentVariables"][k] for k in ("AGENT_CONNECT_TASK_DIR", "AGENT_CONNECT_RESULT_DIR")},
                         {"AGENT_CONNECT_TASK_DIR": f"{self.root}/tasks", "AGENT_CONNECT_RESULT_DIR": f"{self.root}/results"})

    def test_verify_checks_the_running_bridge_against_the_root_the_agent_and_its_token(self):
        fake = self.bridged()
        self.assertEqual(self.install_default(fake)[0], 0)
        state = cli.bridge_home(self.root) / "state"
        self.status(state, agent_id=AGENT, tasks_dir=os.path.realpath(self.root / "tasks"),
                    results_dir=os.path.realpath(self.root / "results"))
        with mock.patch.object(sm, "_run", side_effect=fake):
            rc, out, _ = self.main("verify")
            checks = ConnectVerify.checks(None, out)
            self.assertEqual((checks["bridge"], checks["relay"], checks["hitl"]), ("PASS", "PASS", "PASS"), out)
            self.assertIn(f"PASS bridge unit: {cm.service_path(BA)} ({BA.service_name} active)", out)
            self.assertIn(f"INFO agent: {AGENT}", out)
            (state / "gateway-status.dev.json").write_text("{torn")
            self.assertIn("PASS bridge", self.main("verify")[1], "an unreadable status is no bridge, never a crash")
            for over, why in ((dict(agent_id="@other:x"), "the bridge serves @other:x, not " + AGENT),
                              (dict(results_dir="/elsewhere"), "results go to /elsewhere"),
                              (dict(connected=False, error="401"), "runs but is not connected (401)"),
                              (dict(tasks_dir="/elsewhere/tasks"), "the running bridge feeds /elsewhere/tasks, not"),
                              (dict(tasks_dir=None), "feeds an unreported inbox")):
                self.status(state, **dict(dict(agent_id=AGENT, tasks_dir=os.path.realpath(self.root / "tasks"),
                                               results_dir=os.path.realpath(self.root / "results")), **over))
                out = self.main("verify")[1]
                self.assertIn("FAIL bridge: ", out, over)
                self.assertIn(why, out)
            self.assertIn(f"see journalctl --user -u {BA.service_name}, or run {cli.COMMAND} install --agent {AGENT} "
                          "again", out)
            self.relay.chmod(0o644)
            self.assertEqual(ConnectVerify.checks(None, self.main("verify")[1])["relay"], "FAIL", "the agent's token file")
            cm.service_path(BA).unlink()
            self.assertIn(f"FAIL bridge unit: {cm.service_path(BA)} missing", self.main("verify")[1])

    def test_uninstall_removes_the_marked_bridge_unit_and_keeps_its_state_and_a_foreign_unit_refuses(self):
        fake = self.bridged()
        self.assertEqual(self.install_default(fake)[0], 0)
        fake.calls.clear()
        with mock.patch.object(sm, "_run", side_effect=fake):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.verbs(), ["show", "stop", "disable", "show"])
        self.assertIn(f"removed: {cm.service_path(BA)}", out)
        self.assertLess(out.index(f"removed: {cm.service_path(BA)}"), out.index(f"removed: {cli.config_path(LA)}"))
        self.assertTrue((cli.bridge_home(self.root) / "state").is_dir(), "the bridge's state is kept")
        self.assertIn(f"kept: {self.relay} (Sparrow's token)", out)
        fake = self.bridged()
        self.assertEqual(self.install_default(fake)[0], 0)
        cm.service_path(BA).write_text("[Service]\nExecStart=/mine\n")
        with mock.patch.object(sm, "_run", side_effect=fake):
            rc, _, err = self.main("uninstall")
        self.assertEqual(rc, 1)
        self.assertIn("was not written by install", err)
        self.assertTrue(cli.config_path(LA).exists(), "config last: kept on any refusal")
        rc, _, err = self.install_default(fake)
        self.assertEqual(rc, 2, err)
        self.assertIn("the session is installed but its bridge is not", err)
        cm.service_path(BA).unlink()
        for table, why in (({"show": None}, "state is unknown"), ({"stop": None}, "could not run"),
                           ({"restart": 1}, "restart")):
            fake.table = table
            rc, _, err = self.install_default(fake)
            self.assertEqual(rc, 2, table)
            self.assertIn(why, err)
            fake.table = {}
        fake.table = {"show": None}
        with mock.patch.object(sm, "_run", side_effect=fake):
            rc, _, err = self.main("uninstall")
        self.assertEqual((rc, "state is unknown" in err), (1, True))
        fake.table = {"stop": None}
        with mock.patch.object(sm, "_run", side_effect=fake):
            self.assertEqual(self.main("uninstall")[0], 1)
        fake.table = {}
        with mock.patch.object(sm, "_run", side_effect=fake):
            self.assertEqual(self.main("uninstall")[0], 0)
        with mock.patch.object(cm, "console_script",
                               side_effect=lambda shape: "" if shape == BA else str(self.bin / shape.command)):
            rc, _, err = self.install_default(fake)
        self.assertEqual(rc, 2, err)
        self.assertIn("its bridge is not: ag2-sparrow was not found beside", err)

    def test_install_refuses_an_unknown_agent_a_missing_token_and_a_bridge_already_serving_it(self):
        self.relay.write_text(f"REMOTE_TASK_TOKEN={TOKEN}\n")
        rc, _, err = self.install_default(self.bridged())
        self.assertEqual(rc, 2)
        self.assertIn(f"the agent id is unknown, so its root cannot be named: bind the agent first, or set AGENT_MXID in "
                      f"{self.relay} or the environment, or pass --work <dir>", err)
        self.assertFalse(cli.config_path(LA).exists() or self.agents.exists(), "nothing written")
        with mock.patch.dict(os.environ, {"AGENT_MXID": AGENT}):
            rc, _, err = self.install_default(self.bridged(), "--token-env-file", str(self.home / "none.env"))
        self.assertEqual(rc, 2)
        self.assertIn(f"{self.home / 'none.env'} is missing: put the agent's REMOTE_TASK_TOKEN there", err)
        rc, _, err = self.install_default(self.bridged(), "--work", str(self.work), "--token-env-file", str(self.relay))
        self.assertEqual((rc, "--token-env-file is for the bridge this install runs" in err), (2, True))
        self.relay.write_text(f"REMOTE_TASK_TOKEN={TOKEN}\nAGENT_MXID={AGENT}\n")
        self.status(_dirs.state_dir(), agent_id=AGENT, tasks_dir="/host/tasks")
        rc, _, err = self.install_default(self.bridged())
        self.assertEqual(rc, 2)
        self.assertIn(f"a running bridge already serves {AGENT}", err)
        self.assertIn("one token is one bridge", err)
        self.assertFalse(cli.config_path(LA).exists())

    def test_the_host_binding_names_the_agent_only_for_the_hosts_own_token(self):
        self.relay.write_text(f"REMOTE_TASK_TOKEN={TOKEN}\n")
        auth = _dirs.state_dir() / "auth"
        auth.mkdir(parents=True, exist_ok=True)
        (auth / "ag2space.json").write_text(json.dumps({"agent_id": "@bound:ag2.space"}))
        self.addCleanup((auth / "ag2space.json").unlink, missing_ok=True)
        bound = self.agents / "bound_ag2.space"
        fake = self.bridged(root=bound, shape=cm.for_key(L, cm.agent_key("@bound:ag2.space")))
        rc, _, err = self.install_default(fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cfg()["ROOT"], str(bound), "bind first: the host's binding names its own token's agent")
        other = self.home / "second.env"
        other.write_text(f"REMOTE_TASK_TOKEN={TOKEN}\n")
        with mock.patch.object(sm, "_run", side_effect=fake):
            self.assertEqual(self.main("uninstall")[0], 0)
        rc, _, err = self.install_default(self.bridged(), "--token-env-file", str(other))
        self.assertEqual(rc, 2)
        self.assertIn(f"set AGENT_MXID in {other}", err, "another token never borrows the host's binding")
        other.write_text(f"REMOTE_TASK_TOKEN={TOKEN}\nAGENT_MXID=@second:ag2.space\n")
        second = self.agents / "second_ag2.space"
        rc, _, err = self.install_default(self.bridged(root=second, shape=cm.for_key(L, cm.agent_key("@second:ag2.space"))),
                                          "--token-env-file", str(other))
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.cfg()["ROOT"], self.cfg()["TOKEN_ENV_FILE"]), (str(second), str(other)))
        self.assertEqual(cli.kept_token(self.cfg()), str(other))
        self.assertEqual(cm.kept_line(LA, str(other)), f"kept: {other} (Sparrow's token) and the Claude login",
                         "uninstall names the token file this install configured, not the host's")
        self.assertNotEqual(second, bound, "two agents, two roots")

    def test_a_root_recorded_for_another_agent_is_refused_at_install_and_by_the_launcher(self):
        self.work.mkdir()
        self.assertEqual(inbox.prepare_root(str(self.work)), "")
        records.save_supervisor(str(self.work), dict(records.empty_supervisor(), agent_id="@other:ag2.space"))
        rc, _, err = self.install_with(self.runner(), False, "--work", str(self.work))
        self.assertEqual(rc, 1)
        self.assertIn(f"the work root {self.work} serves the agent @other:ag2.space, not {AGENT}; two agents never share "
                      "a root", err)
        self.assertFalse(cli.config_path(LA).exists(), "refused before anything was applied")
        cfg = dict(self.values(AGENT_ID=AGENT), ROOT=str(self.work))
        lc = launcher.Launcher(cfg, cli.config_dir(LA))
        with mock.patch.object(tmux_pane_keys, "fence_status", return_value="clear"), \
                mock.patch.object(task_io, "holder_consumer", return_value=None):
            ok, problem = lc.create(records.empty_supervisor())
        self.assertEqual((ok, lc.refused), (False, problem), "terminal: the manager stops")
        self.assertIn("serves the agent @other:ag2.space", problem)
        records.supervisor_path(str(self.work)).write_text("[")
        self.assertIn("the work root's record is unreadable", records.agent_problem(str(self.work), AGENT))
        self.assertEqual(records.agent_problem(str(self.home / "fresh"), AGENT), "", "nothing recorded yet")

    def test_an_explicit_root_records_the_hosts_agent_and_an_old_config_reads_as_external(self):
        rc, _, err = self.install_with(self.runner(), False, "--work", str(self.work))
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.cfg()["BRIDGE"], self.cfg()["AGENT_ID"], self.cfg()["TOKEN_ENV_FILE"]), ("external", AGENT, ""))
        self.assertFalse(cm.service_path(BA).exists(), "an explicit root's bridge is yours")
        self.assertEqual(cli.bridge_mode({}), "external", "a config from before BRIDGE existed")

    def test_install_records_the_bridges_state_dir_once_and_verify_reads_the_bridge_there(self):
        """One record, BRIDGE_STATE_DIR: the unit's AGENT_CONNECT_STATE_DIR, verify's bridge row and the HITL
        store all name it; an explicit root names the bridge you run with --bridge-state-dir, so a bridge on
        another state dir passes the row and projects from the store beside it."""
        fake = self.bridged()
        self.assertEqual(self.install_default(fake)[0], 0)
        beside = cli.bridge_home(self.root) / "state"
        self.assertEqual((self.cfg()["BRIDGE_STATE_DIR"], self.cfg()["HITL_STORE"], cli.bridge_state(self.cfg())),
                         (str(beside), str(card.store_root(beside)), beside))
        self.assertIn(f'Environment="AGENT_CONNECT_STATE_DIR={beside}"', cm.service_path(BA).read_text(),
                      "the unit is written from the recorded dir")
        rc, _, err = self.install_default(fake, "--bridge-state-dir", str(self.home / "elsewhere"))
        self.assertEqual(rc, 2)
        self.assertIn(f"--bridge-state-dir is for a bridge you run (an explicit root); this install's bridge keeps its "
                      f"state beside the root, {beside}", err)
        with mock.patch.object(sm, "_run", side_effect=fake):
            self.assertEqual(self.main("uninstall")[0], 0)
        other = self.home / "other-bridge" / "state"
        runner = self.runner()
        rc, _, err = self.install_with(runner, False, "--work", str(self.work), "--bridge-state-dir", str(other))
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.cfg()["BRIDGE"], self.cfg()["BRIDGE_STATE_DIR"], self.cfg()["HITL_STORE"]),
                         ("external", str(other), str(card.store_root(other))))
        self.status(other, agent_id=AGENT)
        with mock.patch.object(sm, "_run", side_effect=runner):
            rc, out, _ = self.main("verify")
            checks = ConnectVerify.checks(None, out)
            self.assertEqual((checks["bridge"], checks["hitl"]), ("PASS", "PASS"), out)
            self.assertIn(f"PASS hitl store: {card.store_root(other)} (the bridge's, beside its default store under {other})", out)
            shutil.rmtree(other)
            out = self.main("verify")[1]
            self.assertIn(f"FAIL bridge: no bridge wrote a status under {other}", out)
            self.assertIn(f"AGENT_CONNECT_STATE_DIR={other}", out, "the fix names the recorded dir")
        runner.stop_session()
        rc, _, err = self.install_with(self.runner(), False, "--work", str(self.work))
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.cfg()["BRIDGE_STATE_DIR"], self.cfg()["HITL_STORE"]),
                         (str(_dirs.state_dir()), str(cli.hitl_store_root())), "without the flag: Sparrow's state dir")
        old = {k: v for k, v in self.cfg().items() if k != "BRIDGE_STATE_DIR"}
        self.assertEqual(cli.bridge_state(old), _dirs.state_dir(), "a config from before the key: the HITL store's")
        self.assertEqual(cli.bridge_state({"HITL_STORE": str(card.store_root(beside))}), beside)
        self.assertEqual(cli.bridge_state({"ROOT": str(self.root), "BRIDGE": "sparrowd"}), beside,
                         "a host supervisor's declaration is written from the dir beside the root, as sparrowd recomputes it")


class ConnectHostSupervised(AgentRootBase):
    """Under a host supervisor (injected through _dirs) install writes a declaration, never a job."""

    def setUp(self):
        super().setUp()
        self.ws = self.home / "ws"
        self.agents = self.ws / "agents"
        self.root = self.agents / "probe.agent_ag2.space"
        saved = _dirs._injected
        _dirs.set_dirs(state_dir=self.ws / "state", agents_dir=self.agents, bridge_host="sparrowd")
        self.addCleanup(setattr, _dirs, "_injected", saved)
        os.environ["AGENT_MXID"] = AGENT

    def decl(self):
        return cli.bridge_home(self.root) / cli.DECLARATION

    def test_the_declaration_points_a_named_worker_at_exactly_the_root_with_the_token_in_the_vault(self):
        fake = self.bridged()
        rc, out, err = self.install_default(fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.calls, [], "no bridge job of its own")
        self.assertFalse(cm.service_path(BA).exists())
        body = json.loads(self.decl().read_text())
        seg = "probe.agent_ag2.space"
        self.assertEqual(body["env"], {"AGENT_CONNECT_TASK_DIR": f"{self.root}/tasks",
                                       "AGENT_CONNECT_RESULT_DIR": f"{self.root}/results",
                                       "AGENT_CONNECT_STATE_DIR": f"{self.root}.bridge/state", "AGENT_MXID": AGENT},
                         "a host supervisor's bridge signs with the host's key: no key path in its declaration")
        self.assertEqual((body["name"], body["token_vault_key"], body["host"], body["module"]),
                         (cli.worker_name(seg), cli.vault_key(seg), "sparrowd", "ag2_sparrow.remote_gateway_bridge"))
        self.assertRegex(body["name"], r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", "sparrowd's worker-name rule")
        self.assertEqual((self.cfg()["BRIDGE"], self.cfg()["TOKEN_VAULT_KEY"], self.cfg()["TOKEN_ENV_FILE"]),
                         ("sparrowd", cli.vault_key(seg), ""))
        self.assertIn(f"vault set {cli.vault_key(seg)} <the agent's relay token>", out)
        self.assertIn(f"if that token is a bare secret, not https://<gateway>|<secret>, also: vault set "
                      f"{cli.url_vault_key(seg)} <the agent's gateway URL>", out)
        self.assertNotIn(TOKEN, self.decl().read_text())
        for a, b in (("probe.agent_ag2.space", "probe-agent_ag2.space"), ("a_b", "a.b")):
            self.assertNotEqual(cli.worker_name(a), cli.worker_name(b), "two agents never share a worker name")
            self.assertNotEqual(cli.vault_key(a), cli.vault_key(b))
        with mock.patch.object(sm, "_run", side_effect=fake):
            out = self.main("verify")[1]
        self.assertIn(f"INFO relay token: the vault key {cli.vault_key(seg)}, read by sparrowd at start", out)
        self.assertIn(f"FAIL bridge: no bridge wrote a status under {self.root}.bridge/state", out)
        self.assertIn(f"sparrowd runs {self.decl()} on its next scan of the declarations: start it if it is not "
                      f"running (src/sparrowd.py)", out)
        self.assertNotIn("relay token:", [ln.split(" ", 1)[1][:12] for ln in out.splitlines() if ln.startswith(("PASS", "FAIL"))])
        with mock.patch.object(sm, "_run", side_effect=fake):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"removed: {self.decl()}", out)
        self.assertFalse(self.decl().exists())
        self.assertIn("kept: the Claude login", out, "the token lives in the vault: no host token file is named")

    def test_the_declaration_is_the_record_install_wrote_before_the_owner_built_it(self):
        rc, out, err = self.install_default(self.bridged())
        self.assertEqual(rc, 0, err)
        seg = "probe.agent_ag2.space"
        before = {"written_by": f"{cli.COMMAND} install", "schema": 1, "host": "sparrowd", "name": cli.worker_name(seg),
                  "module": "ag2_sparrow.remote_gateway_bridge",
                  "env": {"AGENT_CONNECT_TASK_DIR": f"{self.root}/tasks", "AGENT_CONNECT_RESULT_DIR": f"{self.root}/results",
                          "AGENT_CONNECT_STATE_DIR": f"{self.root}.bridge/state", "AGENT_MXID": AGENT},
                  "token_vault_key": cli.vault_key(seg), "token_env": "REMOTE_TASK_TOKEN"}
        self.assertEqual(self.decl().read_text(), json.dumps(before, indent=1, sort_keys=False) + "\n")
        self.assertIn(f"declared: {self.decl()} (sparrowd runs it as worker {cli.worker_name(seg)} on its next scan; ", out)

    def test_a_foreign_declaration_is_never_replaced_or_removed(self):
        self.decl().parent.mkdir(parents=True)
        fake = self.bridged()
        for text in ('{"written_by": "someone else"}', "not json", "[1]"):
            self.decl().write_text(text)
            rc, _, err = self.install_default(fake)
            self.assertEqual(rc, 2, err)
            self.assertIn("was not written by install", err)
            self.assertEqual(self.decl().read_text(), text)
            with mock.patch.object(sm, "_run", side_effect=fake):
                self.assertEqual(self.main("uninstall")[0], 1)
            self.assertTrue(cli.config_path(LA).exists())
        self.decl().unlink()
        with mock.patch.object(sm, "_run", side_effect=fake):
            self.assertEqual(self.main("uninstall")[0], 0)

    def test_the_host_names_no_token_file_and_needs_the_id_from_the_environment(self):
        rc, _, err = self.install_default(self.bridged(), "--token-env-file", str(self.relay))
        self.assertEqual((rc, "--token-env-file under sparrowd is the agent's seat" in err), (2, True))
        os.environ.pop("AGENT_MXID")
        rc, _, err = self.install_default(self.bridged())
        self.assertEqual(rc, 2)
        self.assertIn("set AGENT_MXID in the environment, or pass --work <dir>", err, "the host's binding is its own agent")
        self.status(self.ws / "state", agent_id=AGENT, tasks_dir=str(self.ws / "tasks"))
        os.environ["AGENT_MXID"] = AGENT
        rc, _, err = self.install_default(self.bridged())
        self.assertIn(f"a running bridge already serves {AGENT}", err, "the host's own agent is never a second bridge")

    def test_a_seat_beside_the_root_is_installed_and_one_anywhere_else_is_refused(self):
        from ag2_sparrow import gateway_credential_sources as owner
        os.environ.pop("AGENT_MXID")
        for seat in (self.home / "seat", cli.bridge_home(self.root)):
            self.assertEqual(owner.write_seat_credentials(seat, agent_id=AGENT, instance="f" * 32, tier="team",
                                                          relay_url="https://gw.example/relay", bearer="b-2"), "")
        elsewhere = self.home / "seat" / "channels" / "ag2space" / ".env"
        rc, _, err = self.install_default(self.bridged(), "--token-env-file", str(elsewhere))
        self.assertEqual(rc, 2)
        self.assertIn(f"is the agent's seat, {cli.seat_file(self.root)}", err)
        self.assertFalse(self.decl().exists())
        rc, out, err = self.install_default(self.bridged(), "--token-env-file", cli.seat_file(self.root))
        self.assertEqual(rc, 0, err)
        body = json.loads(self.decl().read_text())
        self.assertEqual((body["token_vault_key"], body["env"]["AG2_DEVICE_ENV"]), ("", cli.seat_file(self.root)))
        self.assertIn(f"with the seat {cli.seat_file(self.root)} and the token file it names", out)
        self.assertNotIn("b-2", self.decl().read_text())

    def test_an_explicit_root_under_the_host_records_only_an_id_from_the_environment(self):
        os.environ.pop("AGENT_MXID")
        rc, _, err = self.install_with(self.runner(), False, "--work", str(self.work))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cfg()["AGENT_ID"], "", "the host's binding is not this root's agent")

SHARED_SESSION_ID = "0b6c4e8e-6d55-4f6e-9a41-8f2a6c1d3e77"


class SharedConfigOptions(unittest.TestCase):
    """--mcp-config and --setting-sources: what a session sharing another config dir loads of it."""

    def test_a_filtered_mcp_set_leaves_the_config_dirs_own_servers_out(self):
        from ag2_sparrow.adapters.claude_code import hold as hold_mod_  # noqa: F401
        with tempfile.TemporaryDirectory() as d:
            ccd = Path(d) / "shared-config"
            ccd.mkdir()
            # The shared dir's own MCP: the core's s.space server and the connected-apps one.
            (ccd / ".claude.json").write_text(json.dumps({"mcpServers": {
                "s-space": {"type": "stdio", "command": "node"}, "sutando-station": {"type": "http", "url": "https://x"}}}))
            mcp = Path(d) / "agent-mcp.json"
            mcp.write_text(json.dumps({"mcpServers": {"sutando-station": {"type": "http", "url": "https://x"}}}))
            cfg = {"CLAUDE": "/opt/claude", "PLUGIN_DIR": "", "CLAUDE_CONFIG_DIR": str(ccd), "MCP_CONFIG": str(mcp),
                   "SETTING_SOURCES": "project,local"}
            argv = hold_mod_.claude_argv(cfg, SHARED_SESSION_ID)
            at = argv.index("--mcp-config")
            self.assertEqual(argv[at - 1:at + 2], ["--strict-mcp-config", "--mcp-config", str(mcp)])
            self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")
            self.assertEqual(argv[-2:], ["--session-id", SHARED_SESSION_ID], "the session id stays last")
            servers, problem = hold_mod_.mcp_servers(cfg["MCP_CONFIG"])
            self.assertEqual((servers, problem), (["sutando-station"], ""))
            self.assertNotIn("s-space", servers, "strict: the config dir's own s.space server is not in the set")
            plain = hold_mod_.claude_argv(dict(cfg, MCP_CONFIG="", SETTING_SOURCES=""), SHARED_SESSION_ID)
            self.assertFalse({"--strict-mcp-config", "--mcp-config", "--setting-sources"} & set(plain))

    def test_bad_mcp_files_and_setting_lists_are_refused(self):
        from ag2_sparrow.adapters.claude_code import cli as cli_, hold as hold_mod_
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.json"
            bad.write_text("[]")
            self.assertIn("no mcpServers", hold_mod_.mcp_servers(str(bad))[1])
            self.assertIn("not a readable MCP config", hold_mod_.mcp_servers(str(Path(d) / "absent.json"))[1])
            for value in ("", "user,admin", "project,project"):
                self.assertTrue(hold_mod_.setting_sources_problem(value), value)
            self.assertEqual(hold_mod_.setting_sources_problem("project,local"), "")
            args = argparse.Namespace(mcp_config=str(bad), setting_sources=None)
            self.assertIn("--mcp-config", cli_.session_files_problem(args))
            self.assertIn("--setting-sources", cli_.session_files_problem(argparse.Namespace(mcp_config=None,
                                                                                           setting_sources="x")))
        recorded = {r[0] for r in cli_.RECORDED}
        self.assertTrue({"MCP_CONFIG", "SETTING_SOURCES", "HMAC_KEY_FILE"} <= recorded, "a repair keeps them")
        self.assertTrue({"MCP_CONFIG", "SETTING_SOURCES"} <= set(cli_.LOCAL.config_keys))


class AdmitReadsOnce(unittest.TestCase):
    """inbox.admit decides on one read of the task file, and only on the text the Mod verified."""

    def test_a_file_changed_since_it_was_verified_is_parked(self):
        import hashlib
        with tempfile.TemporaryDirectory() as root:
            Path(root, "tasks").mkdir()
            task = Path(root, "tasks", "task-v1.txt")
            task.write_bytes(b"id: task-v1\r\naccess_tier: owner\r\ntask: hi\r\n")
            verified = hashlib.sha256(task.read_bytes()).hexdigest()
            kind, headers, _ = inbox.admit(root, "task-v1", False, False, verified)
            self.assertEqual((kind, headers["access_tier"]), ("owner", "owner"), "CRLF parses as a text-mode read")
            task.write_text("id: task-v1\naccess_tier: owner\ntask: rm -rf ~\n")
            with self.assertRaises(inbox.Parked) as cm:
                inbox.admit(root, "task-v1", False, False, verified)
            self.assertIn("changed after its envelope was verified", str(cm.exception))
            self.assertEqual(inbox.admit(root, "task-v1", False, False)[0], "owner", "no hash given: decided as before")


if __name__ == "__main__":
    unittest.main()
