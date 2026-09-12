#!/usr/bin/env python3
"""A worker boots as an instance; the canonical core's ceremony is untouched.

`/startup` is the CORE's bootstrap: orphan recovery over the shared tasks/,
session-cron registration, and a watcher gate satisfied by ANY watcher tree on
the host — the core's own included, which is why a worker running it never
starts the watcher it exists for. It also stamps the core's session-starts.log,
which health-check reads as the current core launch.

Both polarities against the real launcher on a private tmux socket and a copied
repo: unset, every core row and marker is what it was; set, the worker writes
none of them and boots its own mode.

Run: python3 tests/start-cli-worker-bootstrap.test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMUX = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")


# The launcher polls `pgrep -ax claude`, then `ps -o args=` for `--name $SESSION`:
# the stub reports the launched stub's own pid, and the real ps shows its argv.
PGREP_STUB = ('[ "$*" = "-ax claude" ] || exit 0\n'
              '[ -s "$HOME/claude.pid" ] && echo "$(cat "$HOME/claude.pid") claude"\n')
CLAUDE_STUB = 'echo $$ > "$HOME/claude.pid"\nenv > "$HOME/claude.env"\nsleep 120\n'


def _boot(extra_env: dict, server_env: "dict | None" = None, pgrep_stub: str = PGREP_STUB) -> dict:
    """One launcher run against a COPIED repo whose sutando-config.sh names a
    scratch workspace — the real one must never be a test's write target.

    `server_env` starts the tmux server FIRST, from a process carrying that env:
    what a core launch does, since it exports its marker before it touches tmux.

    The pgrep stub answers the liveness probe with nothing until the stub claude
    has recorded its pid; every other probe (the monitor guard) still "finds"
    its target, so no launcher child outlives the run.

    Raises AssertionError unless the launcher's own verdict was success (exit 0):
    a pane left behind by a failed launch is not the state under test.

    Returns the pane argv and env, the session's and server's tmux env, the
    workspace rows, and what the SessionStart hint says to that pane."""
    if not TMUX:
        raise unittest.SkipTest("tmux not found")
    td = Path(tempfile.mkdtemp())
    try:
        root = td / "repo"
        shutil.copytree(REPO / "src", root / "src", symlinks=True)
        shutil.copytree(REPO / "scripts", root / "scripts", symlinks=True)
        ws = td / "ws"
        (ws / "state").mkdir(parents=True)
        (root / "scripts" / "sutando-config.sh").write_text(
            '#!/bin/bash\ncase "$1" in\n'
            '  workspace) echo "%s";;\n'
            '  claude-sutando-config-dir) echo "%s/.claude-sutando";;\n'
            '  python-bin) echo python3;;\n'
            '  core-runtime) echo claude;;\n'
            '  host-label) echo testhost;;\n'
            '  *) echo "";;\nesac\n' % (ws, ws))
        # A live pid in the relay pidfile: that guard passes, so no relay loop
        # outlives this run.
        (ws / "state" / "core-supervisor-relay-loop.pid").write_text(str(os.getpid()))
        bind = td / "bin"
        bind.mkdir()
        (td / "home").mkdir()
        for stub, body in (("claude", CLAUDE_STUB), ("pgrep", pgrep_stub),
                           ("lsof", "exit 1\n"), ("launchctl", "exit 1\n")):
            (bind / stub).write_text("#!/bin/bash\n" + body)
            (bind / stub).chmod(0o755)
        sock = td / "t.sock"
        session = extra_env.get("SUTANDO_TMUX_SESSION", "sutando-core")
        env = {"PATH": f"{bind}:{Path(TMUX).parent}:/usr/bin:/bin:/usr/sbin",
               "HOME": str(td / "home"), "SUTANDO_TMUX_SOCKET": str(sock),
               "SUTANDO_TEST_MODE": "1", **extra_env}

        def tm(*a):
            return subprocess.run([TMUX, "-S", str(sock), *a],
                                  capture_output=True, text=True)
        try:
            if server_env:
                subprocess.run([TMUX, "-S", str(sock), "new-session", "-d", "-s", "seed", "sleep 120"],
                               env={**env, **server_env}, capture_output=True, check=True)
            run = subprocess.run(["/bin/bash", str(root / "src" / "agent" / "claude" / "cli" / "start-cli.sh")],
                                 env=env, capture_output=True, text=True, timeout=90)
            assert run.returncode == 0, (
                f"launcher exited {run.returncode}\nstdout: {run.stdout}\nstderr: {run.stderr}")
            argv = []
            for pid in tm("list-panes", "-s", "-a", "-F", "#{pane_pid}").stdout.split():
                argv += subprocess.run(["ps", "-o", "args=", "-p", pid],
                                       capture_output=True, text=True).stdout.split()
            pane_env = _wait_text(td / "home" / "claude.env")
            hint = subprocess.run(["/bin/bash", str(root / "src" / "schedule-crons-session-hint.sh")],
                                  env=_parse_env(pane_env), capture_output=True, text=True).stdout
            log = ws / "state" / "session-starts.log"
            return {"argv": argv, "pane_env": pane_env, "hint": hint,
                    "session_env": tm("show-environment", "-t", "=" + session).stdout,
                    "global_env": tm("show-environment", "-g").stdout,
                    "session_starts": log.read_text().splitlines() if log.is_file() else []}
        finally:
            tm("kill-server")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _wait_text(path: Path, seconds: float = 5.0) -> str:
    """The pane's shell writes this right after it starts; poll rather than
    read a file the launcher's own session poll may have outrun."""
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text():
            return path.read_text()
        time.sleep(0.05)
    return ""


def _parse_env(dump: str) -> dict:
    return dict(line.split("=", 1) for line in dump.splitlines() if "=" in line)


class TestCorePolarityUnchanged(unittest.TestCase):
    def setUp(self):
        self.got = _boot({})

    def test_the_core_still_boots_the_canonical_ceremony(self):
        self.assertIn("/startup", self.got["argv"])
        self.assertNotIn("--worker", self.got["argv"])

    def test_the_core_still_carries_its_marker_and_logs_its_launch(self):
        self.assertIn("SUTANDO_CORE_SESSION=1", self.got["session_env"])
        self.assertEqual(len(self.got["session_starts"]), 1,
                         "the core's launch row is what health-check reads")


class TestWorkerWritesNoCoreRows(unittest.TestCase):
    def setUp(self):
        wid = "c" * 32
        self.got = _boot({"SUTANDO_INSTANCE_ID": wid,
                          "SUTANDO_TMUX_SESSION": "sutando-worker-" + wid,
                          "SUTANDO_TASKS_DIR": "/tmp/never-read-worker-inbox",
                          "SUTANDO_CLAUDE_SESSION_ID": "11111111-2222-3333-4444-555555555555"})

    def test_the_worker_boots_its_own_mode(self):
        self.assertIn("--worker", self.got["argv"],
                      "the worker ran the canonical core's /startup")
        self.assertIn("/startup", self.got["argv"])

    def test_the_worker_claims_no_core_marker(self):
        """The marker is what makes a session claim the core's bootstrap; the
        SessionStart hook gates cron registration on exactly this."""
        self.assertNotIn("SUTANDO_CORE_SESSION=1", self.got["session_env"])

    def test_the_worker_does_not_retire_the_cores_launch_boundary(self):
        self.assertEqual(self.got["session_starts"], [],
                         "a worker boot was appended to the core's session log")


class TestWorkerOnAServerBornFromACoreLaunch(unittest.TestCase):
    """A tmux server takes its global env from whoever starts it, and a core
    launch exports the marker before its first tmux call; every later session
    on that socket inherits it. The spawner reuses the core's socket, so the
    worker's own session must override the marker — omitting -e does not."""
    def setUp(self):
        wid = "d" * 32
        self.got = _boot({"SUTANDO_INSTANCE_ID": wid,
                          "SUTANDO_TMUX_SESSION": "sutando-worker-" + wid,
                          "SUTANDO_TASKS_DIR": "/tmp/never-read-worker-inbox",
                          "SUTANDO_CLAUDE_SESSION_ID": "11111111-2222-3333-4444-555555555555"},
                         server_env={"SUTANDO_CORE_SESSION": "1"})

    def test_control_the_server_carries_the_core_marker(self):
        self.assertIn("SUTANDO_CORE_SESSION=1", self.got["global_env"])

    def test_the_worker_pane_does_not_inherit_it(self):
        self.assertIn("SUTANDO_INSTANCE_ID=", self.got["pane_env"], "no pane env was captured")
        self.assertNotIn("SUTANDO_CORE_SESSION=1", self.got["pane_env"],
                         "the worker's shell carries the core marker")

    def test_the_session_hint_stays_silent_for_the_worker(self):
        self.assertEqual(self.got["hint"], "",
                         "the worker was told to run the canonical core's /startup")


class TestAFailedLaunchCannotLeaveThisSuiteGreen(unittest.TestCase):
    def test_control_a_liveness_probe_that_reports_nothing_fails_the_fixture(self):
        """The launcher's poll reads `pgrep -ax claude`; a probe that never names
        the launched process is the launcher's own failure verdict (exit 1), and
        the fixture must surface it rather than assert against the pane it left."""
        with self.assertRaisesRegex(AssertionError, r"launcher exited 1[\s\S]*did not come up"):
            _boot({}, pgrep_stub="exit 0\n")


if __name__ == "__main__":
    unittest.main(verbosity=0)
