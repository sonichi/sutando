#!/usr/bin/env python3
"""A core restart must leave a heartbeat writer, and a held cron fire must be visible.

The writer (src/core_heartbeat.py) exits once its core pane has been gone for three
beats. Only startup.sh and the Codex launcher started it, so a Claude core relaunched
by `start-cli.sh --restart` (graceful-restart, the menu-bar restart) ran with no
.alive. cron-runner then held every prompt-backed fire with no log line and no state,
and health-check kept reporting the runner healthy.

Run: python3 tests/core-restart-heartbeat-and-cron-hold.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import redirect_stderr
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMUX = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ClaudeLauncherEnsuresHeartbeat(unittest.TestCase):
    """Real Claude launcher on a private tmux socket; the heartbeat script is a recorder."""

    def setUp(self):
        if not TMUX:
            self.skipTest("tmux not found")
        self.td = Path(tempfile.mkdtemp())
        root = self.td / "repo"
        shutil.copytree(REPO / "src", root / "src", symlinks=True)
        shutil.copytree(REPO / "scripts", root / "scripts", symlinks=True)
        (root / "skills" / "worker-pool" / "scripts").mkdir(parents=True)
        shutil.copy2(REPO / "skills/worker-pool/scripts/launch-worker-session.sh",
                     root / "skills/worker-pool/scripts/launch-worker-session.sh")
        self.calls = self.td / "heartbeat-calls"
        (root / "src" / "core_heartbeat.py").write_text(
            "import sys\n"
            f"open({str(self.calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n")
        ws = self.td / "ws"
        (ws / "state").mkdir(parents=True)
        (root / "scripts" / "sutando-config.sh").write_text(
            '#!/bin/bash\ncase "$1" in\n'
            f'  workspace) echo "{ws}";;\n'
            f'  claude-sutando-config-dir) echo "{ws}/.claude-sutando";;\n'
            '  python-bin) echo python3;;\n'
            '  core-runtime) echo claude;;\n'
            '  host-label) echo testhost;;\n'
            '  *) echo "";;\nesac\n')
        (ws / "state" / "core-supervisor-relay-loop.pid").write_text(str(os.getpid()))
        bind = self.td / "bin"
        bind.mkdir()
        (self.td / "home").mkdir()
        stubs = {
            "claude": 'echo $$ > "$HOME/claude.pid"\nsleep 120\n',
            "pgrep": ('[ "$*" = "-ax claude" ] || exit 0\n'
                      '[ -s "$HOME/claude.pid" ] && echo "$(cat "$HOME/claude.pid") claude"\n'),
            "lsof": "exit 1\n", "launchctl": "exit 1\n",
        }
        for name, body in stubs.items():
            (bind / name).write_text("#!/bin/bash\n" + body)
            (bind / name).chmod(0o755)
        self.root = root
        self.sock = self.td / "t.sock"
        self.env = {"PATH": f"{bind}:{Path(TMUX).parent}:/usr/bin:/bin:/usr/sbin",
                    "HOME": str(self.td / "home"), "SUTANDO_TMUX_SOCKET": str(self.sock),
                    "SUTANDO_TEST_MODE": "1", "SUTANDO_RESTART_GRACE_S": "2"}

    def tearDown(self):
        subprocess.run([TMUX, "-S", str(self.sock), "kill-server"], capture_output=True)
        shutil.rmtree(self.td, ignore_errors=True)

    def _launch(self, *args):
        run = subprocess.run(["/bin/bash", str(self.root / "src/agent/claude/cli/start-cli.sh"), *args],
                             env=self.env, capture_output=True, text=True, timeout=90)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def _ensures(self) -> int:
        lines = self.calls.read_text().splitlines() if self.calls.exists() else []
        return sum(1 for line in lines if line.strip() == "--ensure")

    def test_fresh_launch_and_restart_each_ensure_the_writer(self):
        self._launch()
        self.assertEqual(self._ensures(), 1, "a fresh core launch did not ensure the heartbeat")
        self._launch("--restart")
        self.assertEqual(self._ensures(), 2, "--restart relaunched the core with no heartbeat writer")
        calls = [line.strip() for line in self.calls.read_text().splitlines()]
        # The old writer is still inside its absence grace window during a restart: an ensure
        # alone would adopt it, and it would exit a minute later. It must be stopped first.
        self.assertEqual(calls, ["--ensure", "--stop", "--ensure"], calls)

    def test_rerun_against_a_live_core_heals_a_missing_writer(self):
        self._launch()
        self._launch()
        self.assertEqual(self._ensures(), 2, "an idempotent re-run did not re-ensure the heartbeat")


class EnsureRunningIsIdempotent(unittest.TestCase):
    """The real `--ensure` from a copied checkout: one writer, then a no-op."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.root = self.td / "repo"
        shutil.copytree(REPO / "src", self.root / "src", symlinks=True)
        shutil.copytree(REPO / "scripts", self.root / "scripts", symlinks=True)
        self.env = {"PATH": "/usr/bin:/bin:/usr/sbin", "HOME": str(self.td),
                    # A socket no server listens on: the writer must find no core.
                    "SUTANDO_TMUX_SOCKET": str(self.td / "none.sock")}
        self.started: list[int] = []

    def tearDown(self):
        for pid in self.started:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(self.td, ignore_errors=True)

    def _ensure(self) -> str:
        r = subprocess.run([sys.executable, str(self.root / "src/core_heartbeat.py"), "--ensure",
                            "--log", str(self.td / "heartbeat.log")],
                           env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pid = int(r.stdout.rsplit("(pid ", 1)[1].rstrip(")\n"))
        if pid:
            self.started.append(pid)
        return r.stdout

    def test_second_ensure_finds_the_first_writer(self):
        first = self._ensure()
        self.assertIn("started writer", first)
        second = self._ensure()
        self.assertIn("writer already running", second)
        self.assertEqual(len(set(self.started)), 1, f"two writers for one checkout: {self.started}")

    def test_a_relative_argv_writer_is_found_through_its_pidfile(self):
        # The skill-started writer runs as `python3 src/core_heartbeat.py` from the repo root;
        # an argv match on the absolute path misses it and would start a duplicate.
        ws = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'src');"
                             "from workspace_default import resolve_workspace; print(resolve_workspace())"],
                            cwd=self.root, env=self.env, capture_output=True, text=True).stdout.strip()
        (Path(ws) / "state" / "cores").mkdir(parents=True, exist_ok=True)
        with open(self.td / "rel.log", "w") as log:
            proc = subprocess.Popen([sys.executable, "src/core_heartbeat.py"], cwd=self.root,
                                    env=self.env, stdout=log, stderr=subprocess.STDOUT)
        self.started.append(proc.pid)
        self.addCleanup(proc.wait)
        pidfile = next(iter((Path(ws) / "state" / "cores").glob("*.heartbeat.pid")), None)
        deadline = time.monotonic() + 10
        while pidfile is None and time.monotonic() < deadline:
            time.sleep(0.1)
            pidfile = next(iter((Path(ws) / "state" / "cores").glob("*.heartbeat.pid")), None)
        self.assertIsNotNone(pidfile, (self.td / "rel.log").read_text())
        out = self._ensure()
        self.assertIn("writer already running", out)
        self.assertIn(f"(pid {proc.pid})", out)

    def _workspace(self, root: Path) -> Path:
        ws = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'src');"
                             "from workspace_default import resolve_workspace; print(resolve_workspace())"],
                            cwd=root, env=self.env, capture_output=True, text=True).stdout.strip()
        (Path(ws) / "state" / "cores").mkdir(parents=True, exist_ok=True)
        return Path(ws)

    def _relative_writer(self, root: Path, tag: str) -> subprocess.Popen:
        with open(self.td / f"{tag}.log", "w") as log:
            proc = subprocess.Popen([sys.executable, "src/core_heartbeat.py"], cwd=root,
                                    env=self.env, stdout=log, stderr=subprocess.STDOUT)
        self.started.append(proc.pid)
        self.addCleanup(proc.wait)
        return proc

    def _stop(self) -> str:
        r = subprocess.run([sys.executable, str(self.root / "src/core_heartbeat.py"), "--stop"],
                           env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_stop_ends_a_relative_argv_writer_of_this_checkout(self):
        # A pre-upgrade writer runs as `python3 src/core_heartbeat.py`; --restart's handoff must
        # end it, or it survives into the new core's grace window and then exits with no writer.
        ws = self._workspace(self.root)
        proc = self._relative_writer(self.root, "rel")
        pidfile = ws / "state" / "cores"
        deadline = time.monotonic() + 10
        while not any(pidfile.glob("*.heartbeat.pid")) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(any(pidfile.glob("*.heartbeat.pid")), (self.td / "rel.log").read_text())
        self.assertIn("stopped 1 writer(s)", self._stop())
        self.assertIsNotNone(proc.wait(timeout=10), "the relative-argv writer survived --stop")

    def test_stop_leaves_another_checkouts_relative_writer_alone(self):
        other = self.td / "other"
        shutil.copytree(self.root / "src", other / "src", symlinks=True)
        shutil.copytree(self.root / "scripts", other / "scripts", symlinks=True)
        self._workspace(other)
        proc = self._relative_writer(other, "other")
        time.sleep(1)
        ws = self._workspace(self.root)
        # This checkout's .alive names the foreign pid; its cwd resolves to the other checkout.
        host = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'src');"
                               "import core_heartbeat; print(core_heartbeat._alive_path().name)"],
                              cwd=self.root, env=self.env, capture_output=True, text=True).stdout.strip()
        (ws / "state" / "cores" / host).write_text(json.dumps({"heartbeat_pid": proc.pid}))
        self.assertIn("stopped 0 writer(s)", self._stop())
        self.assertIsNone(proc.poll(), "--stop killed another checkout's writer")

    def test_writer_exits_when_its_checkout_is_removed(self):
        self._ensure()
        pid = self.started[0]
        time.sleep(2)  # past module import, into the beat loop
        os.kill(pid, 0)
        shutil.rmtree(self.root)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            # A reaped child of ours never lingers; an orphan is reparented and reaped by init.
            time.sleep(0.5)
        self.fail("writer outlived its checkout")


class CronRunnerHoldIsLoud(unittest.TestCase):
    def setUp(self):
        self.cr = _load("cron_runner_hold", REPO / "src" / "cron-runner.py")
        self.td = Path(tempfile.mkdtemp())
        cr = self.cr
        cr.TASKS_DIR = self.td / "tasks"
        cr.CRONS_FILE = self.td / "crons.json"
        cr.STATE_FILE = self.td / "state" / "cron-runner-state.json"
        cr.CORE_ALIVE_FILE = self.td / "state" / "cores" / "testhost.alive"
        cr.CRONS_FILE.write_text(json.dumps([
            {"name": "digest", "cron": "* * * * *", "prompt": "run digest", "launchd": True}]))
        self.hold = self.td / "state" / "cron-runner-hold.json"

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _tick(self, now: int) -> tuple[list, str]:
        err = io.StringIO()
        with redirect_stderr(err):
            emitted = self.cr.run(now)
        return emitted, err.getvalue()

    def test_missing_heartbeat_records_and_logs_the_hold_then_clears_it(self):
        now = int(time.time())
        emitted, err = self._tick(now)
        self.assertEqual(emitted, [])
        self.assertIn("holding due fire(s) digest", err)
        self.assertIn("core heartbeat missing", err)
        record = json.loads(self.hold.read_text())
        self.assertEqual(record["held"], ["digest"])
        self.assertEqual(record["since"], now)
        # A later held tick keeps the start of the outage.
        self._tick(now + 60)
        self.assertEqual(json.loads(self.hold.read_text())["since"], now)
        # Heartbeat back: the fire goes out and the hold record is gone.
        self.cr.CORE_ALIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.cr.CORE_ALIVE_FILE.write_text("{}")
        os.utime(self.cr.CORE_ALIVE_FILE, (now + 120, now + 120))
        emitted, _ = self._tick(now + 120)
        self.assertEqual(emitted, ["digest"])
        self.assertFalse(self.hold.exists(), "hold record survived a fresh heartbeat")


class HealthCheckReportsTheHold(unittest.TestCase):
    def test_a_held_runner_is_not_reported_healthy(self):
        health = _load("health_check_hold", REPO / "src" / "health-check.py")
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "workspace"
            cfg = ws / "hosts" / "test-host" / "crons.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps([{"name": "digest", "cron": "2 6 * * *",
                                        "prompt": "run", "launchd": True}]))
            state = ws / "state" / "cron-runner-state.json"
            state.parent.mkdir(parents=True)
            state.write_text("{}")
            os.utime(state, (1000, 1000))
            ok = lambda _: {"status": "ok"}
            self.assertEqual(health.check_cron_runner(ws, "test-host", "claude", ok, now=1030)["status"], "ok")
            (ws / "state" / "cron-runner-hold.json").write_text(json.dumps(
                {"since": 600, "updated_at": 1000, "held": ["digest"],
                 "reason": "core heartbeat missing (x.alive)"}))
            check = health.check_cron_runner(ws, "test-host", "claude", ok, now=1030)
            self.assertEqual(check["status"], "down", check)
            self.assertIn("digest", check["detail"])
            self.assertIn("core heartbeat missing", check["detail"])
            (ws / "state" / "cron-runner-hold.json").write_text(json.dumps(
                {"since": 1000, "updated_at": 1000, "held": ["digest"], "reason": "r"}))
            self.assertEqual(health.check_cron_runner(ws, "test-host", "claude", ok, now=1030)["status"], "warn")


def _cp(rc: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["x"], rc, out, "")


class EnsureInProcess(unittest.TestCase):
    """The --ensure machinery called directly, with process I/O faked: nothing is spawned."""

    def setUp(self):
        sys.path.insert(0, str(REPO / "src"))
        import core_heartbeat
        self.ch = core_heartbeat
        self.td = Path(tempfile.mkdtemp())
        self._saved = {k: getattr(core_heartbeat, k) for k in (
            "WORKSPACE", "_recorded_writer_pids", "running_writer_pids", "ensure_running", "_SCRIPT")}
        core_heartbeat.WORKSPACE = self.td
        core_heartbeat._recorded_writer_pids = lambda: []

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.ch, k, v)
        shutil.rmtree(self.td, ignore_errors=True)

    def _fake_run(self, pgrep, args_by_pid):
        def run(cmd, *a, **kw):
            if cmd[0] == "pgrep":
                if isinstance(pgrep, Exception):
                    raise pgrep
                return pgrep
            pid = int(cmd[-1])
            val = args_by_pid.get(pid, "")
            if isinstance(val, Exception):
                raise val
            return _cp(0, val + "\n")
        return run

    def test_writer_argv_forms(self):
        ch, s = self.ch, str(self.ch._SCRIPT)
        self.assertTrue(ch._writer_argv(f"/usr/bin/python3 {s}"))
        self.assertTrue(ch._writer_argv("/x/Python.app/Contents/MacOS/Python src/core_heartbeat.py"))
        self.assertTrue(ch._writer_argv(f"python3 {s} --interval 5"))
        self.assertFalse(ch._writer_argv(f"python3 {s} --ensure"))
        self.assertFalse(ch._writer_argv("python3 -c 'core_heartbeat.py'"))
        self.assertFalse(ch._writer_argv("bash src/core_heartbeat.py"))

    def test_this_checkouts_writer_resolves_relative_argv_through_cwd(self):
        ch, s = self.ch, str(self.ch._SCRIPT)
        root = str(self.ch._SCRIPT.parent.parent)
        self.assertTrue(ch._this_checkouts_writer(9, f"python3 {s} --interval 60"))
        self.assertFalse(ch._this_checkouts_writer(9, f"python3 {s} --stop"))
        rel = "python3 src/core_heartbeat.py"
        with unittest.mock.patch.object(ch, "_pid_cwd", return_value=root):
            self.assertTrue(ch._this_checkouts_writer(9, rel))
        with unittest.mock.patch.object(ch, "_pid_cwd", return_value=str(self.td)):
            self.assertFalse(ch._this_checkouts_writer(9, rel), "another checkout's writer")
        with unittest.mock.patch.object(ch, "_pid_cwd", return_value=None):
            self.assertFalse(ch._this_checkouts_writer(9, rel), "an unverifiable cwd is left alone")

    def test_pid_cwd_falls_back_to_lsof(self):
        ch = self.ch
        self.assertEqual(Path(ch._pid_cwd(os.getpid()) or "").resolve(), Path.cwd().resolve())
        with unittest.mock.patch.object(ch.os, "readlink", side_effect=OSError), \
             unittest.mock.patch.object(ch.subprocess, "run", return_value=_cp(0, "p1\nfcwd\nn/some/dir\n")):
            self.assertEqual(ch._pid_cwd(1), "/some/dir")
        with unittest.mock.patch.object(ch.os, "readlink", side_effect=OSError), \
             unittest.mock.patch.object(ch.subprocess, "run", side_effect=OSError("no lsof")):
            self.assertIsNone(ch._pid_cwd(1))

    def test_running_writer_pids_filters_candidates(self):
        ch, s = self.ch, str(self.ch._SCRIPT)
        ch._recorded_writer_pids = lambda: [1, 501]
        args = {501: "python3 src/core_heartbeat.py", 502: f"python3 {s}", 503: f"python3 {s} --stop",
                504: OSError("ps gone")}
        pg = _cp(0, f"502\n503\n504\n{os.getpid()}\n")
        with unittest.mock.patch.object(ch.subprocess, "run", self._fake_run(pg, args)):
            self.assertEqual(ch.running_writer_pids(), [501, 502])

    def test_running_writer_pids_pgrep_outcomes(self):
        ch = self.ch
        ch._recorded_writer_pids = lambda: [601]
        args = {601: "python3 src/core_heartbeat.py"}
        with unittest.mock.patch.object(ch.subprocess, "run", self._fake_run(_cp(1), args)):
            self.assertEqual(ch.running_writer_pids(), [601])
        with unittest.mock.patch.object(ch.subprocess, "run", self._fake_run(OSError("no pgrep"), args)):
            self.assertEqual(ch.running_writer_pids(), [601])
        # A matched-but-silent pgrep is never read as "no writer".
        with unittest.mock.patch.object(ch.subprocess, "run", self._fake_run(_cp(0, ""), args)):
            self.assertEqual(ch.running_writer_pids(), [0])

    def test_ensure_adopts_an_existing_writer(self):
        ch = self.ch
        ch.running_writer_pids = lambda: [777]
        with unittest.mock.patch.object(ch.subprocess, "Popen") as popen:
            self.assertEqual(ch.ensure_running(self.td / "hb.log"), (False, 777))
        popen.assert_not_called()
        self.assertTrue((self.td / "state" / "locks" / "core-heartbeat-ensure.lock").exists())

    def test_ensure_spawns_and_waits_for_the_child_argv(self):
        ch = self.ch
        answers = iter([[], [], [4242]])
        ch.running_writer_pids = lambda: next(answers)
        child = unittest.mock.Mock(pid=4242)
        child.poll.return_value = None
        with unittest.mock.patch.object(ch.subprocess, "Popen", return_value=child) as popen, \
                unittest.mock.patch.object(ch.time, "sleep"):
            self.assertEqual(ch.ensure_running(self.td / "hb.log"), (True, 4242))
        argv = popen.call_args[0][0]
        self.assertEqual(argv, [sys.executable, str(ch._SCRIPT)])
        self.assertTrue(popen.call_args[1]["start_new_session"])

    def test_ensure_stops_waiting_for_a_child_that_exited(self):
        ch = self.ch
        ch.running_writer_pids = lambda: []
        child = unittest.mock.Mock(pid=4343)
        child.poll.return_value = 1
        with unittest.mock.patch.object(ch.subprocess, "Popen", return_value=child):
            self.assertEqual(ch.ensure_running(self.td / "hb.log"), (True, 4343))
        child.poll.assert_called_once()

    def test_main_ensure_reports_both_outcomes(self):
        ch = self.ch
        seen = []
        for result, word in (((True, 11), "started writer"), ((False, 12), "writer already running")):
            ch.ensure_running = lambda log, r=result: (seen.append(log), r)[1]
            out = io.StringIO()
            with unittest.mock.patch.object(sys, "stdout", out):
                self.assertEqual(ch.main(["--ensure", "--log", str(self.td / "x.log")]), 0)
            self.assertIn(word, out.getvalue())
        self.assertEqual(seen, [self.td / "x.log"] * 2)

    def test_run_forever_leaves_when_its_checkout_is_gone(self):
        ch = self.ch
        ch._SCRIPT = self.td / "removed" / "core_heartbeat.py"
        old = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT), ch._alive_path,
               ch._SHUTDOWN_REQUESTED)
        ch._alive_path = lambda: self.td / "h.alive"
        ch._SHUTDOWN_REQUESTED = False
        try:
            with unittest.mock.patch.object(ch, "core_pid") as core_pid:
                self.assertEqual(ch.run_forever(interval=0.01), 0)
            core_pid.assert_not_called()
        finally:
            signal.signal(signal.SIGTERM, old[0])
            signal.signal(signal.SIGINT, old[1])
            ch._alive_path, ch._SHUTDOWN_REQUESTED = old[2], old[3]


class CronRunnerStaleReason(unittest.TestCase):
    def test_a_stale_heartbeat_is_named_stale_with_its_age(self):
        cr = _load("cron_runner_reason", REPO / "src" / "cron-runner.py")
        with tempfile.TemporaryDirectory() as td:
            cr.CORE_ALIVE_FILE = Path(td) / "h.alive"
            self.assertIn("missing", cr._core_alive_reason(time.time()))
            cr.CORE_ALIVE_FILE.write_text("{}")
            os.utime(cr.CORE_ALIVE_FILE, (1000, 1000))
            self.assertEqual(cr._core_alive_reason(1500),
                             f"core heartbeat stale (500s old, limit {cr.CORE_ALIVE_MAX_AGE_SECONDS}s)")


if __name__ == "__main__":
    unittest.main()
