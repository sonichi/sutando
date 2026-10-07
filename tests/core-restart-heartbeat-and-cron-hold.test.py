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


if __name__ == "__main__":
    unittest.main()
