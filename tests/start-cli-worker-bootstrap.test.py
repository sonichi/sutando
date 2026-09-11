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


def _boot(extra_env: dict) -> dict:
    """One launcher run against a COPIED repo whose sutando-config.sh names a
    scratch workspace — the real one must never be a test's write target.

    Returns the pane argv, the session's tmux env, and the workspace rows."""
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
        # A live pid in the relay pidfile, and a pgrep that always "finds" the
        # monitor: both guards pass, so no launcher child outlives this run.
        (ws / "state" / "core-supervisor-relay-loop.pid").write_text(str(os.getpid()))
        bind = td / "bin"
        bind.mkdir()
        (td / "home").mkdir()
        for stub, body in (("claude", "sleep 120\n"), ("pgrep", "exit 0\n"),
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
            subprocess.run(["/bin/bash", str(root / "src" / "agent" / "claude" / "cli" / "start-cli.sh")],
                           env=env, capture_output=True, text=True, timeout=90)
            argv = []
            for pid in tm("list-panes", "-s", "-a", "-F", "#{pane_pid}").stdout.split():
                argv += subprocess.run(["ps", "-o", "args=", "-p", pid],
                                       capture_output=True, text=True).stdout.split()
            log = ws / "state" / "session-starts.log"
            return {"argv": argv,
                    "session_env": tm("show-environment", "-t", "=" + session).stdout,
                    "session_starts": log.read_text().splitlines() if log.is_file() else []}
        finally:
            tm("kill-server")
    finally:
        shutil.rmtree(td, ignore_errors=True)


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


if __name__ == "__main__":
    unittest.main(verbosity=0)
