#!/usr/bin/env python3
"""The Claude launcher runs the task notifier under the supervisor, in
`<session>-watcher`, for the core only, and tears it down on --restart.

Real launcher, real tmux on a private socket, a copied repo whose
sutando-config.sh names a scratch workspace, and a stub `claude`.
Run: python3 tests/start-cli-claude-task-notifier.test.py
The heal-path, watcher-identity and replace/mode classes live in the -heal,
-identity and -modes files beside this one; each imports the Harness from here.
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
TMUX = shutil.which("tmux") or shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
PGREP_STUB = ('[ "$*" = "-ax claude" ] || exit 0\n'
              '[ -s "$HOME/claude.pid" ] && echo "$(cat "$HOME/claude.pid") claude"\n')
CLAUDE_STUB = '[ "$1" = --version ] && exit 0\necho $$ > "$HOME/claude.pid"\nsleep 120\n'


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
        for name in ("launch-worker-session.sh", "worker-mcp-credential.sh"):
            shutil.copy2(REPO / "skills/worker-pool/scripts" / name, root / "skills/worker-pool/scripts" / name)
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
                    "SUTANDO_TEST_MODE": "1",
                    # The stub claude never answers --version; the Mod has its own launch tests.
                    "SUTANDO_TASK_WATCH_MOD_CORE": "0"}

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

    def test_turning_the_stop_interrupt_on_and_back_off_replaces_the_watcher(self):
        for wanted in ("1", ""):
            created_before = self.h.watcher_created()
            time.sleep(1.1)   # session_created has 1 s resolution
            run = self.h.launch(extra_env={"SUTANDO_STOP_INTERRUPT": wanted} if wanted else None)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            _, _, env = self.h.watcher()
            self.assertNotEqual(created_before, self.h.watcher_created(), f"switch {wanted!r}: a new watcher")
            self.assertEqual("SUTANDO_STOP_INTERRUPT=1" in env, wanted == "1", "and it carries the switch, or not")

    def test_a_stale_server_global_switch_never_reaches_a_watcher_launched_without_it(self):
        self.h.tm("set-environment", "-g", "SUTANDO_STOP_INTERRUPT", "1")
        self.h.tm("kill-session", "-t", "=sutando-core-watcher")
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        out = self.h.td / "effective.env"
        self.h.tm("new-window", "-d", "-t", "=sutando-core-watcher", f"printenv SUTANDO_STOP_INTERRUPT > {out}; echo done >> {out}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "done" not in (out.read_text() if out.exists() else ""):
            time.sleep(0.1)
        self.assertEqual(out.read_text().splitlines()[0], "", "the session's explicit empty value wins over the global 1")

    def test_the_alert_knobs_are_always_set_so_a_server_global_value_never_reaches_the_watcher(self):
        self.h.tm("set-environment", "-g", "SUTANDO_NOTIFIER_ALERT_GRACE", "7")
        self.h.tm("set-environment", "-g", "SUTANDO_NOTIFIER_ALERT_REPEAT", "0")
        self.h.tm("kill-session", "-t", "=sutando-core-watcher")
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        _, _, env = self.h.watcher()
        lines = env.splitlines()
        self.assertIn("SUTANDO_NOTIFIER_ALERT_GRACE=", lines)
        self.assertIn("SUTANDO_NOTIFIER_ALERT_REPEAT=", lines)


if __name__ == "__main__":
    unittest.main(verbosity=2)
