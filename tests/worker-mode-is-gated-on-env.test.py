#!/usr/bin/env python3
"""The pool's two edits to existing scripts are invisible to the core.

`start-cli.sh` gains a worker mode and `watch-tasks-stream.sh` gains a
delivery-folder override; both are gated on env the core never sets. These
tests pin the gate from both sides: unset, the core's launch and the core's
watched folder are what they were; set, the worker's are what the pool needs.

Run: python3 tests/worker-mode-is-gated-on-env.test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"


def _launch_argv(extra_env: dict) -> list[str]:
    """start-cli.sh through its real tmux path on a private socket; returns the
    argv of the process it put in the pane. A stub claude that persists is what
    keeps the session alive long enough to read it."""
    import shutil
    tmux = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if not tmux:
        raise unittest.SkipTest("tmux not found")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bind = td / "bin"; bind.mkdir(); (td / "home").mkdir(); (td / "workspace" / "state").mkdir(parents=True)
        sock = td / "t.sock"
        for stub, body in (("claude", "sleep 300\n"), ("pgrep", "exit 0\n")):
            (bind / stub).write_text("#!/bin/bash\n" + body); (bind / stub).chmod(0o755)
        tm = lambda *a: subprocess.run([tmux, "-S", str(sock), *a], capture_output=True, text=True)
        env = {"PATH": f"{bind}:{Path(tmux).parent}:/usr/bin:/bin:/usr/sbin", "HOME": str(td / "home"),
               "SUTANDO_TMUX_SOCKET": str(sock), "SUTANDO_TEST_MODE": "1",
               "SUTANDO_WORKSPACE": str(td / "workspace"), **extra_env}
        try:
            subprocess.run(["/bin/bash", str(LAUNCHER)], env=env, capture_output=True, text=True, timeout=60)
            argv = []
            for pid in tm("list-panes", "-s", "-a", "-F", "#{pane_pid}").stdout.split():
                argv += subprocess.run(["ps", "-o", "args=", "-p", pid], capture_output=True, text=True).stdout.split()
            return argv
        finally:
            tm("kill-server")


class TestLauncherGate(unittest.TestCase):
    def test_unset_the_core_launch_keeps_its_owner_surfaces(self):
        argv = _launch_argv({})
        self.assertTrue(argv, "claude was never exec'd")
        self.assertIn("--remote-control", argv)
        self.assertIn("--chrome", argv)
        self.assertNotIn("--session-id", argv)
        self.assertIn("sutando-core", argv)

    def test_set_the_worker_launch_drops_them_and_binds_its_session(self):
        argv = _launch_argv({"SUTANDO_INSTANCE_ID": "a" * 32, "SUTANDO_TMUX_SESSION": "sutando-worker-" + "a" * 32,
                             "SUTANDO_TASKS_DIR": "/tmp/never-read", "SUTANDO_CLAUDE_SESSION_ID": "11111111-2222-3333-4444-555555555555"})
        self.assertTrue(argv, "claude was never exec'd")
        self.assertNotIn("--remote-control", argv)
        self.assertNotIn("--chrome", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(argv[argv.index("--name") + 1], "sutando-worker-" + "a" * 32)
        self.assertNotIn("sutando-core", " ".join(argv))


def _watched_dir(extra_env: dict, td: Path) -> tuple[bool, bool]:
    """Run a private copy of the watcher for a moment; report which folder it
    created: (the workspace's tasks/, the override).

    The watcher is a process GROUP (bash, fswatch, a sleep loop); killing the
    leader alone leaves children writing into the tree while it is removed."""
    import signal
    root = td / "repo"
    shutil.copytree(REPO / "src", root / "src", symlinks=True)
    shutil.copytree(REPO / "scripts", root / "scripts", symlinks=True)
    ws = td / "ws"; ws.mkdir()
    (root / "scripts" / "sutando-config.sh").write_text('#!/bin/bash\ncase "$1" in workspace) echo "%s";; python-bin) echo python3;; *) echo "";; esac\n' % ws)
    env = {**os.environ, "SUTANDO_RESULTS_DIR": str(ws / "results"), **extra_env}
    if "SUTANDO_TASKS_DIR" not in extra_env:
        env.pop("SUTANDO_TASKS_DIR", None)
    p = subprocess.Popen(["bash", str(root / "src" / "watch-tasks-stream.sh")], cwd=str(root), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.time() + 6
        while time.time() < deadline and not ((ws / "tasks").is_dir() or (td / "deliveries").is_dir()) and p.poll() is None:
            time.sleep(0.1)
        return (ws / "tasks").is_dir(), (td / "deliveries").is_dir()
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        time.sleep(0.2)


class TestWatcherGate(unittest.TestCase):
    def test_unset_the_core_watches_its_workspace_tasks_folder(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            core, override = _watched_dir({}, Path(td))
        self.assertTrue(core, "the core's tasks/ was not the watched folder")
        self.assertFalse(override)

    def test_set_a_worker_watches_its_delivery_folder_only(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            core, override = _watched_dir({"SUTANDO_TASKS_DIR": str(Path(td) / "deliveries")}, Path(td))
        self.assertTrue(override, "the delivery folder was not the watched folder")
        self.assertFalse(core, "the worker must not create or watch the core's tasks/")


if __name__ == "__main__":
    unittest.main(verbosity=0)
