#!/usr/bin/env python3
"""`pool_remedy --restart <id>`: the owner's restart of one worker, outside the death ladder.

One JSON line `{worker_id, result, detail}`; result is restarted | already-running | paused |
suspended | failed; exit 0 except failed (1) and an invalid id (2). A restart or an
already-running worker clears that worker's ladder evidence only, so an escalated worker
starts clean; any other result leaves the ladder alone.

Run: python3 tests/skills/worker-pool/pool-restart.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


rem = _load("pool_remedy")
ps = rem.ps
WID, OTHER = "a" * 32, "b" * 32


class RestartSessionContinuity(unittest.TestCase):
    """Real launcher/tmux boundary with a synthetic endpoint, not a model conversation."""

    def setUp(self):
        for tool in ("tmux", "cc"):
            if not shutil.which(tool):
                self.skipTest(f"{tool} is required for the real launcher regression")
        tmp = tempfile.TemporaryDirectory(prefix="restart-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.ws, self.socket = self.root / "workspace", self.root / "tmux.sock"
        self.ws.mkdir()
        self.endpoint = self.root / "bin"
        self.endpoint.mkdir()
        runtime_root = Path(os.environ.get("SUTANDO_RUNTIME_SOCKET", str(self.root / "api.sock"))).parent
        self.diagnostics = runtime_root / "diagnostics" if (runtime_root / "diagnostics").is_dir() else self.root
        source = runtime_root / "runtime-endpoint.c"
        if not source.is_file():
            source = self.root / "synthetic-runtime.c"
            source.write_text("""
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
static volatile sig_atomic_t done;
static void stop(int sig) { (void)sig; done = 1; }
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--version")) {
        puts("synthetic runtime endpoint; no model"); return 0;
    }
    signal(SIGTERM, stop); signal(SIGINT, stop); signal(SIGHUP, stop);
    puts("synthetic endpoint ready; no conversation or authentication");
    fflush(stdout);
    while (!done) sleep(1);
    return 0;
}
""")
        subprocess.run(["cc", str(source), "-o", str(self.endpoint / "claude")],
                       check=True, capture_output=True, text=True, timeout=30)
        env = {"PATH": str(self.endpoint) + os.pathsep + os.environ["PATH"],
               "SUTANDO_TEST_MODE": "1", "SUTANDO_TELEMETRY": "0",
               "SUTANDO_WORKSPACE": str(self.ws), "SUTANDO_WORKSPACE_DIR": str(self.ws),
               "SUTANDO_DEFAULT_WORKSPACE": str(self.ws),
               "SUTANDO_RESULTS_DIR": str(self.ws / "results"),
               "SUTANDO_RUNTIME_STATE": str(self.ws / "state"),
               "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
               "SUTANDO_TMUX_SOCKET": str(self.socket),
               "SUTANDO_NOTIFIER_GRACE_PERIOD": "300", "SUTANDO_NOTIFIER_ROLE_POLL": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for target, name, value in ((rem.sw, "ensure_remedy_timer", {"installed": False}),
                                    (rem, "ensure_input_watch", {"outcome": "test-suppressed"})):
            patcher = mock.patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server_log = open(self.diagnostics / "restart-tmux.log", "w+")
        self.addCleanup(self.server_log.close)
        self.server = subprocess.Popen(["tmux", "-D", "-f", "/dev/null", "-S", str(self.socket)],
                                       cwd=REPO, env=dict(os.environ), stdin=subprocess.DEVNULL,
                                       stdout=self.server_log, stderr=subprocess.STDOUT)
        self.pids = set()
        self.addCleanup(self.stop_server)
        self.wait_for(lambda: self.socket.exists() or self.server.poll() is not None)
        self.assertIsNone(self.server.poll(), "foreground tmux exited before socket readiness")
        self.assertEqual(self.tmux("show-options", "-g", "exit-empty").returncode, 0)
        keeper = self.tmux("new-session", "-d", "-s", "test-keeper", str(self.endpoint / "claude"))
        self.assertEqual(keeper.returncode, 0, keeper.stderr)

    def wait_for(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for disposable runtime state")

    def tmux(self, *args):
        return subprocess.run(["tmux", "-S", str(self.socket), *args], env=dict(os.environ),
                              capture_output=True, text=True, timeout=10)

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def stop_server(self):
        panes = self.tmux("list-panes", "-a", "-F", "#{pane_pid}")
        self.pids.update(int(p) for p in panes.stdout.split() if p.isdigit())
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.wait_for(lambda: all(not self.alive(pid) for pid in self.pids))
        finally:
            self.tmux("kill-server")
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=5)
        self.assertIsNotNone(self.server.returncode)
        (self.diagnostics / "restart-cleanup.json").write_text(json.dumps({
            "server_pid": self.server.pid, "server_reaped": True,
            "pane_pids": sorted(self.pids), "panes_absent": all(not self.alive(p) for p in self.pids)}))

    def call_main(self, module, args, log_name):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", err):
            rc = module.main(["--workspace", str(self.ws), "--repo", str(REPO), *args])
        (self.diagnostics / log_name).write_text(out.getvalue() + err.getvalue())
        self.assertEqual(rc, 0, err.getvalue() or out.getvalue())
        return json.loads(out.getvalue())

    def observe(self, name, session, folder):
        pane = self.tmux("list-panes", "-t", "=" + name, "-F", "#{pane_pid}\t#{pane_current_path}\t#{pane_id}")
        self.assertEqual(pane.returncode, 0, pane.stderr)
        pid, cwd, pane_id = pane.stdout.strip().split("\t")
        self.pids.add(int(pid))
        proc = subprocess.run(["ps", "-p", pid, "-o", "args="], env=dict(os.environ),
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = shlex.split(proc.stdout)
        self.assertEqual(Path(args[0]).name, "claude")
        self.assertEqual(args[args.index("--resume") + 1], session)
        self.assertNotIn("--session-id", args)
        self.assertEqual(Path(cwd).resolve(), folder)
        capture = self.tmux("capture-pane", "-p", "-t", pane_id)
        self.assertEqual(capture.returncode, 0, capture.stderr)
        self.assertIn("synthetic endpoint ready", capture.stdout)
        return {"pid": int(pid), "argv": args, "cwd": cwd}

    def test_explicit_resume_then_restart_keeps_a_and_its_recorded_folder(self):
        wi, sw = rem.wi, rem.sw
        a_dir, b_dir = self.root / "project-a", self.root / "project-b"
        a_dir.mkdir()
        b_dir.mkdir()
        first = wi.create_worker(self.ws, runtime="claude", cwd=str(a_dir),
                                 tmux_socket=str(self.socket))
        wid, a = first["worker_id"], first["runtime_session_id"]
        wi.end_incarnation(self.ws, wid, first["incarnation_id"], "exited")
        wi.record_session(self.ws, wid, "later-session-b", runtime="claude",
                          relation=wi.RELATION_NEW, cwd=str(b_dir))
        sw.pr.register_worker(self.ws, wid, "alpha", room="test-room", runtime="claude")
        lineage, roster = wi.sessions(self.ws, wid), sw.pr.load_roster(self.ws)
        spawned = self.call_main(sw, ["--resume", a, "--socket", str(self.socket)], "restart-spawn.json")
        self.assertEqual(spawned["worker_id"], wid)
        self.assertEqual(spawned["label"], "alpha")
        name = wi.tmux_session_name(wid)
        before = self.observe(name, a, a_dir)
        os.kill(before["pid"], signal.SIGTERM)
        self.wait_for(lambda: not self.alive(before["pid"]))
        self.wait_for(lambda: sw.session_state(name, str(self.socket)) == "absent")
        runs = wi.incarnations(self.ws, wid)
        with mock.patch.dict(os.environ, {"SUTANDO_TMUX_SOCKET": str(self.root / "wrong.sock")}):
            restarted = self.call_main(rem, ["--restart", wid], "restart-result.json")
        self.assertEqual(restarted["result"], "restarted")
        self.assertEqual(restarted["detail"]["session_id"], a)
        after = self.observe(name, a, a_dir)
        self.assertNotEqual(after["pid"], before["pid"])
        now = wi.incarnations(self.ws, wid)
        self.assertEqual(len(now), len(runs) + 1)
        self.assertEqual(now[-1]["session_id"], a)
        self.assertEqual(now[-1]["tmux"]["socket"], str(self.socket))
        self.assertEqual(wi.sessions(self.ws, wid), lineage)
        self.assertEqual(sw.pr.load_roster(self.ws), roster)
        again = self.call_main(rem, ["--restart", wid], "restart-live.json")
        self.assertEqual(again["result"], "already-running")
        self.assertEqual(wi.incarnations(self.ws, wid), now)
        self.assertEqual(self.observe(name, a, a_dir)["pid"], after["pid"])
        (self.diagnostics / "restart-continuity.json").write_text(json.dumps({
            "synthetic_endpoint": True, "socket": str(self.socket), "worker_id": wid,
            "before": before, "after": after, "result": restarted, "already_running": again}, indent=2))


class Restart(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.escalated = ps.WorkerEvidence(consecutive=351, escalated=True, recover_issued_at=5.0)
        self.other = ps.WorkerEvidence(consecutive=2)
        rem.sup.save_state(self.ws, ps.SupervisionState(last_sample_at=1.0, workers={
            WID: self.escalated, OTHER: self.other}))
        self.ensure = mock.patch.object(rem, "ensure_supervisor", return_value={"outcome": rem.SUPERVISED})
        self.watch = mock.patch.object(rem, "ensure_input_watch", return_value={"outcome": rem.WATCHING})
        self.sup_mock, self.watch_mock = self.ensure.start(), self.watch.start()

    def tearDown(self):
        mock.patch.stopall()
        self.td.cleanup()

    def run_main(self, recovered):
        out = io.StringIO()
        with mock.patch.object(rem, "recover", return_value=recovered), redirect_stdout(out):
            rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--restart", WID])
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        return rc, json.loads(lines[0])

    def ladder(self):
        return rem.sup.load_state(self.ws).workers

    def test_an_escalated_worker_restarts_now_and_its_ladder_starts_clean(self):
        rc, out = self.run_main({"worker_id": WID, "outcome": rem.RECOVERED, "session_id": "s1"})
        self.assertEqual(rc, 0)
        self.assertEqual((out["worker_id"], out["result"]), (WID, "restarted"))
        self.assertEqual(out["detail"], {"session_id": "s1", "supervisor": rem.SUPERVISED,
                                         "input_watch": rem.WATCHING})
        self.assertNotIn(WID, self.ladder())
        self.assertEqual(self.ladder()[OTHER], self.other)
        self.sup_mock.assert_called_once(), self.watch_mock.assert_called_once()

    def test_a_running_worker_is_left_alone_and_loses_only_its_death_ladder(self):
        rc, out = self.run_main({"worker_id": WID, "outcome": rem.ALREADY_RUNNING})
        self.assertEqual((rc, out["result"]), (0, "already-running"))
        ev = self.ladder()[WID]
        self.assertEqual((ev.consecutive, ev.escalated, ev.recover_issued_at), (0, False, None))

    def test_a_wedged_live_worker_keeps_its_wedge_and_watcher_ladders(self):
        wedged = ps.WorkerEvidence(consecutive=3, wedge_consecutive=9, wedge_escalated=True,
                                   wedge_kind="frozen", wedge_first_detected_at=4.0,
                                   watcher_consecutive=2, watcher_escalated=True)
        rem.sup.save_state(self.ws, ps.SupervisionState(last_sample_at=1.0, workers={WID: wedged}))
        rc, out = self.run_main({"worker_id": WID, "outcome": rem.ALREADY_RUNNING})
        ev = self.ladder()[WID]
        self.assertEqual((rc, ev.consecutive), (0, 0))
        self.assertEqual((ev.wedge_escalated, ev.wedge_kind, ev.wedge_consecutive), (True, "frozen", 9))
        self.assertEqual((ev.watcher_escalated, ev.watcher_consecutive), (True, 2))

    def test_resume_and_restart_share_the_ladder_reset(self):
        with mock.patch.object(rem, "_reset_ladders", return_value=None) as reset:
            self.run_main({"worker_id": WID, "outcome": rem.RECOVERED})
        reset.assert_called_once_with(str(self.ws), forget=[WID])
        rem.suspend(self.ws, "app-quit", now=1)
        dead = ps.Observation(beat=ps.STALE, session_alive=False, paused=False)
        with mock.patch.object(rem, "_reset_ladders", return_value=None) as reset, \
                mock.patch.object(rem.sup, "observe", return_value={WID: dead}), \
                mock.patch.object(rem, "_marker", return_value={"stopped": [WID]}), \
                mock.patch.object(rem, "recover", return_value={"worker_id": WID, "outcome": rem.RECOVERED}):
            rem.resume(self.ws, REPO)
        self.assertEqual(list(reset.call_args.kwargs["forget"]), [WID])

    def test_a_paused_worker_is_not_restarted_and_keeps_its_ladder(self):
        rc, out = self.run_main({"worker_id": WID, "outcome": rem.PAUSED})
        self.assertEqual((rc, out["result"]), (0, "paused"))
        self.assertEqual(self.ladder()[WID], self.escalated)
        self.sup_mock.assert_not_called()

    def test_a_suspended_pool_restarts_nothing(self):
        rem.suspend(self.ws, "app-quit", now=1)
        with mock.patch.object(rem, "recover") as rec:
            rc, out = self.run_main({"worker_id": WID, "outcome": rem.RECOVERED})
        rec.assert_not_called()
        self.assertEqual((rc, out["result"]), (0, "suspended"))
        self.assertEqual(self.ladder()[WID], self.escalated)

    def test_every_other_outcome_is_failed_exits_one_and_names_why(self):
        for outcome in (rem.FAILED, rem.NO_SESSION, rem.INDETERMINATE):
            with self.subTest(outcome=outcome):
                rc, out = self.run_main({"worker_id": WID, "outcome": outcome, "why": "x"})
                self.assertEqual((rc, out["result"]), (1, "failed"))
                self.assertEqual(out["detail"], {"why": "x", "outcome": outcome})
                self.assertEqual(self.ladder()[WID], self.escalated)
                rc, out = self.run_main({"worker_id": WID, "outcome": outcome})
                self.assertEqual(out["detail"]["why"], rem.FAILED_WHY[outcome])

    def test_a_bookkeeping_crash_after_the_restart_is_reported_not_raised(self):
        self.sup_mock.side_effect = OSError("disk full")
        with mock.patch.object(rem, "_reset_ladders", side_effect=OSError("read-only")):
            rc, out = self.run_main({"worker_id": WID, "outcome": rem.RECOVERED})
        self.assertEqual((rc, out["result"]), (0, "restarted"))
        self.assertEqual(out["detail"]["errors"], {"ladder": "OSError: read-only", "supervisor": "OSError: disk full"})
        self.assertEqual(out["detail"]["input_watch"], rem.WATCHING)

    def test_any_unexpected_error_is_still_one_json_line(self):
        with mock.patch.object(rem, "restart", side_effect=RuntimeError("boom")):
            rc, out = self.run_main({})
        self.assertEqual((rc, out), (1, {"worker_id": WID, "result": "failed",
                                         "detail": {"why": "RuntimeError: boom"}}))

    def test_an_empty_id_is_the_json_reply_with_exit_two(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--restart", ""])
        self.assertEqual((rc, json.loads(out.getvalue())["result"]), (2, "failed"))

    def test_restart_refuses_dry_run(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--restart", WID, "--dry-run"])
        self.assertIn("no --dry-run", err.getvalue())

    def test_an_invalid_id_is_failed_with_exit_two(self):
        out = io.StringIO()
        with mock.patch.object(rem, "recover") as rec, redirect_stdout(out):
            rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--restart", "../escape"])
        rec.assert_not_called()
        self.assertEqual(rc, 2)
        got = json.loads(out.getvalue())
        self.assertEqual((got["worker_id"], got["result"]), ("../escape", "failed"))
        self.assertIn("worker id must match", got["detail"]["why"])

    def test_restart_is_one_mode_among_the_others(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--restart", WID, "--sweep"])
        self.assertIn("--resume or --restart", err.getvalue())


class Lock(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "ws"
        (self.ws / "state").mkdir(parents=True)

    def tearDown(self):
        self.td.cleanup()

    def hold(self, seconds):
        held, release = threading.Event(), threading.Event()

        def run():
            with rem.pool_lock(self.ws):
                held.set()
                release.wait(seconds)
        t = threading.Thread(target=run)
        t.start()
        held.wait(5)
        return t, release

    def test_a_restart_waits_for_a_sweep_then_acts(self):
        t, release = self.hold(5)
        threading.Timer(0.5, release.set).start()
        started = time.monotonic()
        with mock.patch.object(rem, "recover", return_value={"worker_id": WID, "outcome": rem.PAUSED}) as rec:
            out = rem.restart(self.ws, REPO, WID, lock_timeout=5)
        t.join()
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        rec.assert_called_once()
        self.assertEqual(out["result"], "paused")

    def test_a_restart_that_cannot_get_the_lock_fails_without_acting(self):
        t, release = self.hold(5)
        try:
            with mock.patch.object(rem, "recover") as rec:
                out = rem.restart(self.ws, REPO, WID, lock_timeout=0.3)
        finally:
            release.set(), t.join()
        rec.assert_not_called()
        self.assertEqual(out["result"], "failed")
        self.assertIn("pool busy", out["detail"]["why"])

    def test_the_sweep_holds_the_same_lock_for_its_whole_act_and_save(self):
        seen = []

        def sweep(a):
            with self.assertRaises(rem.PoolBusy):
                with rem.pool_lock(self.ws, timeout=0.1):
                    pass
            seen.append(a.sweep)
            return 0
        with mock.patch.object(rem, "_sweep", side_effect=sweep):
            rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--sweep"])
        self.assertEqual((rc, seen), (0, [True]))


if __name__ == "__main__":
    sys.exit(unittest.main())
