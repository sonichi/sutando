#!/usr/bin/env python3
"""The Claude launcher runs the task notifier under the supervisor, in
`<session>-watcher`, for the core only, and tears it down on --restart.

Real launcher, real tmux on a private socket, a copied repo whose
sutando-config.sh names a scratch workspace, and a stub `claude`.
Run: python3 tests/start-cli-claude-task-notifier.test.py
The heal-path and watcher-identity classes live in the -heal and -identity
files beside this one; both import the Harness from here.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMUX = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
PGREP_STUB = ('[ "$*" = "-ax claude" ] || exit 0\n'
              '[ -s "$HOME/claude.pid" ] && echo "$(cat "$HOME/claude.pid") claude"\n')
CLAUDE_STUB = 'echo $$ > "$HOME/claude.pid"\nsleep 120\n'


class Harness:
    def __init__(self, claude_body: str = CLAUDE_STUB) -> None:
        if not TMUX:
            raise unittest.SkipTest("tmux not found")
        self.td = Path(tempfile.mkdtemp())
        root = self.td / "repo"
        shutil.copytree(REPO / "src", root / "src", symlinks=True)
        shutil.copytree(REPO / "scripts", root / "scripts", symlinks=True)
        # Only the worker's own launcher, not the whole skill: this harness
        # tests the launch mechanics, not worker-pool's own behavior.
        (root / "skills" / "worker-pool" / "scripts").mkdir(parents=True)
        shutil.copy2(REPO / "skills/worker-pool/scripts/launch-worker-session.sh",
                    root / "skills/worker-pool/scripts/launch-worker-session.sh")
        self.root = root
        ws = self.td / "ws"
        (ws / "state").mkdir(parents=True)
        (root / "scripts" / "sutando-config.sh").write_text(
            '#!/bin/bash\ncase "$1" in\n'
            '  workspace) echo "%s";;\n'
            '  claude-sutando-config-dir) echo "%s/.claude-sutando";;\n'
            '  python-bin) echo python3;;\n'
            '  core-runtime) echo claude;;\n'
            '  host-label) echo testhost;;\n'
            '  *) echo "";;\nesac\n' % (ws, ws))
        (ws / "state" / "core-supervisor-relay-loop.pid").write_text(str(os.getpid()))
        bind = self.td / "bin"
        bind.mkdir()
        (self.td / "home").mkdir()
        for stub, body in (("claude", claude_body), ("pgrep", PGREP_STUB),
                           ("lsof", "exit 1\n"), ("launchctl", "exit 1\n")):
            (bind / stub).write_text("#!/bin/bash\n" + body)
            (bind / stub).chmod(0o755)
        self.sock = self.td / "t.sock"
        self.env = {"PATH": f"{bind}:{Path(TMUX).parent}:/usr/bin:/bin:/usr/sbin",
                    "HOME": str(self.td / "home"), "SUTANDO_TMUX_SOCKET": str(self.sock),
                    "SUTANDO_TEST_MODE": "1"}

    def tm(self, *a):
        return subprocess.run([TMUX, "-S", str(self.sock), *a], capture_output=True, text=True)

    def launch(self, *args, extra_env=None):
        run = subprocess.run(["/bin/bash", str(self.root / "src/agent/claude/cli/start-cli.sh"), *args],
                             env={**self.env, **(extra_env or {})}, capture_output=True, text=True, timeout=90)
        return run

    def launch_worker(self, *args, extra_env=None):
        run = subprocess.run(["/bin/bash", str(self.root / "skills/worker-pool/scripts/launch-worker-session.sh"), *args],
                             env={**self.env, **(extra_env or {})}, capture_output=True, text=True, timeout=90)
        return run

    def watcher(self, session="sutando-core"):
        """(exists, pane_start_command, session env) for <session>-watcher."""
        name = f"{session}-watcher"
        exists = self.tm("has-session", "-t", f"={name}").returncode == 0
        cmd = self.tm("list-panes", "-t", f"={name}", "-F", "#{pane_start_command}").stdout.strip() if exists else ""
        env = self.tm("show-environment", "-t", f"={name}").stdout if exists else ""
        return exists, cmd, env

    def watcher_created(self, session="sutando-core"):
        rows = self.tm("list-sessions", "-F", "#{session_name} #{session_created}").stdout.split("\n")
        hits = [r.split(" ", 1)[1] for r in rows if r.startswith(f"{session}-watcher ")]
        assert hits, "no watcher session to read session_created from"
        return hits[0]

    def close(self):
        self.tm("kill-server")
        shutil.rmtree(self.td, ignore_errors=True)


class CoreLaunchStartsSupervisedNotifier(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def tearDown(self):
        self.h.close()

    def test_watcher_session_runs_the_supervisor_with_the_claude_notifier(self):
        exists, cmd, env = self.h.watcher()
        self.assertTrue(exists, "no sutando-core-watcher session after a core launch")
        self.assertIn("task-notifier-supervisor.sh", cmd)
        self.assertIn("SUTANDO_NOTIFIER_SCRIPT=" + str(self.h.root / "src/agent/claude/cli/task-notifier.sh"), env)
        self.assertIn("SUTANDO_NOTIFIER_VERSION=", env)
        self.assertIn("SUTANDO_TMUX_SESSION=sutando-core", env)

    def test_rerun_keeps_the_same_watcher_session(self):
        created_before = self.h.watcher_created()
        time.sleep(1.1)   # session_created has 1 s resolution
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        exists, _, _ = self.h.watcher()
        self.assertTrue(exists)
        self.assertEqual(created_before, self.h.watcher_created(),
                         "an idempotent re-run replaced the watcher session")

    def test_restart_tears_the_watcher_down_with_the_core(self):
        created_before = self.h.watcher_created()
        time.sleep(1.1)
        run = self.h.launch("--restart", extra_env={"SUTANDO_RESTART_GRACE_S": "2"})
        # --restart recreates the core, and with it a fresh watcher; the old one must be gone
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        exists, cmd, _ = self.h.watcher()
        self.assertTrue(exists, "restart left no watcher for the new core")
        self.assertIn("task-notifier-supervisor.sh", cmd)
        self.assertNotEqual(created_before, self.h.watcher_created(),
                            "restart kept the old watcher session alive")
        sessions = self.h.tm("list-sessions", "-F", "#{session_name}").stdout.split()
        self.assertEqual(sessions.count("sutando-core-watcher"), 1)


class LauncherForwardsOnlyAGenuinePin(unittest.TestCase):
    """Since #4503, the launcher resolves nothing: the watcher declares/reads
    its own handler via a config file it fswatches. The launcher's only job
    left is to forward a pin the operator already set, verbatim."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_no_pin_no_handler_reaches_the_watcher(self):
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        _, _, env = self.h.watcher()
        self.assertNotIn("SUTANDO_TASK_EVENT_HANDLER=", env)

    def test_an_explicit_pin_reaches_the_watcher_verbatim(self):
        run = self.h.launch(extra_env={"SUTANDO_TASK_EVENT_HANDLER": "/opt/handler"})
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        _, _, env = self.h.watcher()
        self.assertIn("SUTANDO_TASK_EVENT_HANDLER=/opt/handler", env)


class WorkerLaunchStartsNoNotifier(unittest.TestCase):
    """A worker no longer even CAN reach start-cli.sh's notifier/monitor
    machinery: it has its own launcher (skills/worker-pool/scripts/
    launch-worker-session.sh), which contains no ensure_task_notifier or
    ensure_core_monitor call at all -- not a guard that happens to return
    early, an absent call site. Real launcher, real tmux, same rigor as the
    core-side tests above."""

    def test_worker_launch_creates_no_watcher_session(self):
        h = Harness()
        try:
            wid = "c" * 32
            delivery_writer = h.td / "pool_delivery.py"
            delivery_writer.write_text("# Test-only writer path for the worker launcher.\n")
            run = h.launch_worker(extra_env={"SUTANDO_INSTANCE_ID": wid,
                                             "SUTANDO_TMUX_SESSION": "sutando-worker-" + wid,
                                             "SUTANDO_TASKS_DIR": "/tmp/never-read-worker-inbox",
                                             "SUTANDO_POOL_DELIVERY_SCRIPT": str(delivery_writer),
                                             "SUTANDO_CLAUDE_SESSION_ID": "11111111-2222-3333-4444-555555555555"})
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            sessions = h.tm("list-sessions", "-F", "#{session_name}").stdout.split()
            self.assertIn("sutando-worker-" + wid, sessions, sessions)
            self.assertFalse(any(s.endswith("-watcher") for s in sessions), sessions)
        finally:
            h.close()

    def test_the_worker_launcher_has_no_notifier_or_monitor_call_site(self):
        """Source-tied guard, same shape as the core-side tests: if a future
        edit reintroduces a notifier/monitor call into the worker's own
        launcher, this fails even if the live-tmux test above somehow didn't
        exercise the new path."""
        worker_launcher = (REPO / "skills/worker-pool/scripts/launch-worker-session.sh").read_text()
        for forbidden in ("ensure_task_notifier", "ensure_core_monitor"):
            self.assertNotIn(forbidden, worker_launcher,
                             f"the worker launcher calls {forbidden}, which is core-only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
