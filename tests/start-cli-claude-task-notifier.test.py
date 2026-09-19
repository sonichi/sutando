#!/usr/bin/env python3
"""The Claude launcher runs the task notifier under the supervisor, in
`<session>-watcher`, for the core only, and tears it down on --restart.

Real launcher, real tmux on a private socket, a copied repo whose
sutando-config.sh names a scratch workspace, and a stub `claude`.
Run: python3 tests/start-cli-claude-task-notifier.test.py
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


class HealPathStartsTheNotifierOnlyForALiveCore(unittest.TestCase):
    """A sibling window keeps the session alive while the core window is gone;
    the heal recreates the core at index 0. The watcher targets that index, so
    it may exist only once the healed process is proven alive."""

    def _session_with_sibling_only(self, h: Harness) -> None:
        # The server inherits this env; a window healed later must see the harness HOME.
        subprocess.run([TMUX, "-S", str(h.sock), "new-session", "-d", "-s", "sutando-core",
                        "-n", "core", "sleep 120"], env=h.env, check=True)
        h.tm("new-window", "-d", "-t", "sutando-core", "-n", "gateway", "sleep 120")
        h.tm("kill-window", "-t", "=sutando-core:0")   # index 0 freed, session survives

    def test_a_healed_core_that_dies_at_once_leaves_no_watcher(self):
        h = Harness(claude_body="exit 0\n")   # no pid file: the core is never seen alive
        try:
            self._session_with_sibling_only(h)
            run = h.launch()
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("healed window did not come up", run.stderr)
            sessions = h.tm("list-sessions", "-F", "#{session_name}").stdout.split()
            self.assertIn("sutando-core", sessions, "the sibling window must keep the session")
            self.assertNotIn("sutando-core-watcher", sessions,
                             "a failed heal left a watcher aimed at a session with no core")
        finally:
            h.close()

    def test_a_healed_core_that_lives_gets_the_watcher(self):
        h = Harness()
        try:
            self._session_with_sibling_only(h)
            run = h.launch()
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertNotIn("healed window did not come up", run.stderr)
            exists, cmd, env = h.watcher()
            self.assertTrue(exists, "a live healed core must get its watcher")
            self.assertIn("task-notifier-supervisor.sh", cmd)
            self.assertIn("SUTANDO_TMUX_WINDOW=0", env)
        finally:
            h.close()

    def test_a_core_that_dies_after_the_poll_takes_its_watcher_with_it(self):
        # The core outlives the launcher's poll, then exits; the sibling keeps the
        # session alive. The supervisor must gate on the core pane, not the session.
        h = Harness(claude_body='echo $$ > "$HOME/claude.pid"\nsleep 3\n')
        try:
            self._session_with_sibling_only(h)
            run = h.launch()
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            exists, _, _ = h.watcher()
            self.assertTrue(exists, "the watcher starts while the core is alive")
            deadline = time.time() + 12
            while time.time() < deadline and h.watcher()[0]:
                time.sleep(0.5)
            sessions = h.tm("list-sessions", "-F", "#{session_name}").stdout.split()
            self.assertIn("sutando-core", sessions, "the sibling keeps the session")
            self.assertNotIn("sutando-core-watcher", sessions,
                             "a watcher outlived the core it was aimed at")
        finally:
            h.close()

    def test_a_core_healed_beside_a_sibling_at_index_0_is_targeted_by_index(self):
        # The gateway sits at :0, so the heal lands the core at :1. A watcher
        # aimed at :0 would type owner tasks into the gateway.
        h = Harness()
        try:
            subprocess.run([TMUX, "-S", str(h.sock), "new-session", "-d", "-s", "sutando-core",
                            "-n", "gateway", "sleep 120"], env=h.env, check=True)
            run = h.launch()
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            idx = h.tm("list-windows", "-t", "=sutando-core", "-F", "#{window_index} #{window_name}").stdout
            self.assertIn("0 gateway", idx)
            self.assertIn("1 ", idx, "the core must have landed at index 1: " + idx)
            exists, _, env = h.watcher()
            self.assertTrue(exists)
            self.assertIn("SUTANDO_TMUX_WINDOW=1", env,
                          "the watcher must target the healed core's window, not :0")
        finally:
            h.close()

    def test_the_task_handler_env_reaches_a_watcher_on_an_existing_server(self):
        h = Harness()
        try:
            self._session_with_sibling_only(h)
            run = h.launch(extra_env={"SUTANDO_TASK_EVENT_HANDLER": "/opt/handler"})
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            _, _, env = h.watcher()
            self.assertIn("SUTANDO_TASK_EVENT_HANDLER=/opt/handler", env,
                          "a required Team handler must be forwarded to the watcher")
        finally:
            h.close()


class WatcherIdentityTests(unittest.TestCase):
    """The watcher's identity is the exact core pane plus its configuration; a
    rerun, a config change, or a replacement pane must each be seen."""

    def _sibling_at_zero(self, h: Harness) -> None:
        subprocess.run([TMUX, "-S", str(h.sock), "new-session", "-d", "-s", "sutando-core",
                        "-n", "gateway", "sleep 120"], env=h.env, check=True)

    def test_a_plain_rerun_keeps_the_healed_target(self):
        h = Harness()
        try:
            self._sibling_at_zero(h)
            run = h.launch(); self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            _, _, env1 = h.watcher()
            self.assertIn("SUTANDO_TMUX_WINDOW=1", env1)
            pane1 = [l for l in env1.splitlines() if l.startswith("SUTANDO_TMUX_PANE=")]
            self.assertTrue(pane1, "no pane identity recorded: " + env1)
            time.sleep(1.1)
            run = h.launch(); self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            _, _, env2 = h.watcher()
            self.assertIn("SUTANDO_TMUX_WINDOW=1", env2, "a rerun aimed the watcher back at :0")
            self.assertEqual(pane1, [l for l in env2.splitlines() if l.startswith("SUTANDO_TMUX_PANE=")])
        finally:
            h.close()

    def test_a_newly_configured_handler_reaches_an_existing_watcher(self):
        h = Harness()
        try:
            run = h.launch(); self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            _, _, env1 = h.watcher()
            self.assertNotIn("SUTANDO_TASK_EVENT_HANDLER=", env1)
            run = h.launch(extra_env={"SUTANDO_TASK_EVENT_HANDLER": "/opt/handler"})
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            _, _, env2 = h.watcher()
            self.assertIn("SUTANDO_TASK_EVENT_HANDLER=/opt/handler", env2,
                          "a handler configured after the watcher started never reached it")
        finally:
            h.close()

    def test_the_watcher_runs_the_launcher_resolved_interpreter(self):
        h = Harness()
        try:
            run = h.launch(); self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            _, _, env = h.watcher()
            py = [l.split("=", 1)[1] for l in env.splitlines() if l.startswith("SUTANDO_NOTIFIER_PY=")]
            self.assertTrue(py and py[0].startswith("/"), "interpreter not passed as an absolute path: " + env)
        finally:
            h.close()

    def test_no_runnable_python_starts_the_core_but_no_watcher_and_says_so(self):
        # Without the developer tools the PATH python3 is the CLT stub whose every run raises
        # a dialog; the resolver answers nothing, and a bare `python3` fallback would run it each second.
        h = Harness()
        try:
            (h.root / "scripts" / "python-binary.sh").write_text(
                "resolve_python() { :; }\nrequire_python() { return 1; }\n")
            run = h.launch(); self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(h.tm("has-session", "-t", "=sutando-core").returncode, 0, "the core itself must still start")
            exists, _, _ = h.watcher()
            self.assertFalse(exists, "a watcher was started with no runnable interpreter")
            self.assertIn("no runnable python3", run.stderr)
        finally:
            h.close()

    def test_a_replacement_pane_in_the_cores_window_does_not_keep_the_watcher(self):
        # The core pane exits while a sibling pane keeps the same window alive.
        h = Harness()
        try:
            run = h.launch(); self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertTrue(h.watcher()[0])
            h.tm("split-window", "-d", "-t", "=sutando-core:0", "sleep 120")
            core_pid = (h.td / "home" / "claude.pid").read_text().strip()
            subprocess.run(["kill", core_pid], check=False)
            deadline = time.time() + 12
            while time.time() < deadline and h.watcher()[0]:
                time.sleep(0.5)
            windows = h.tm("list-windows", "-t", "=sutando-core", "-F", "#{window_index}").stdout.split()
            self.assertIn("0", windows, "the sibling pane must keep window 0")
            self.assertFalse(h.watcher()[0], "a watcher outlived its pane because the window index survived")
        finally:
            h.close()

    def test_window_creation_failure_exits_66_with_no_watcher(self):
        h = Harness()
        try:
            self._sibling_at_zero(h)
            # A tmux that refuses only new-window, in front of the real one on PATH.
            wrapper = h.td / "bin" / "tmux"
            wrapper.write_text('#!/bin/bash\nfor a in "$@"; do [ "$a" = new-window ] && exit 1; done\n'
                               f'exec "{TMUX}" "$@"\n')
            wrapper.chmod(0o755)
            run = h.launch()
            self.assertEqual(run.returncode, 66, run.stdout + run.stderr)
            sessions = h.tm("list-sessions", "-F", "#{session_name}").stdout.split()
            self.assertIn("sutando-core", sessions)
            self.assertNotIn("sutando-core-watcher", sessions)
        finally:
            h.close()


class PoolRouteHandlerReachesTheWatcher(unittest.TestCase):
    """The launcher defaults the watcher's task-event handler from the optional
    worker-pool skill, so a restart never arms a watcher that routes nothing."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def _install_skill(self, name="pool"):
        script = self.h.root / "skills" / name / "scripts" / "route_handler.py"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        link = self.h.root / "skills" / name / "task-event-handler"
        link.symlink_to("scripts/route_handler.py")
        return link

    def test_without_the_skill_no_handler_is_set(self):
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        _, _, env = self.h.watcher()
        self.assertNotIn("SUTANDO_TASK_EVENT_HANDLER=", env)

    def test_with_the_skill_the_handler_reaches_the_watcher(self):
        p = self._install_skill()
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        _, _, env = self.h.watcher()
        self.assertIn(f"SUTANDO_TASK_EVENT_HANDLER={p}", env)

    def test_an_explicit_handler_wins_over_the_skill_default(self):
        self._install_skill()
        run = self.h.launch(extra_env={"SUTANDO_TASK_EVENT_HANDLER": "/opt/handler"})
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        _, _, env = self.h.watcher()
        self.assertIn("SUTANDO_TASK_EVENT_HANDLER=/opt/handler", env)

    def test_two_publishing_skills_are_ambiguous_and_set_nothing(self):
        self._install_skill("pool-a")
        self._install_skill("pool-b")
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("2 skills publish one", run.stderr)
        _, _, env = self.h.watcher()
        self.assertNotIn("SUTANDO_TASK_EVENT_HANDLER=", env)

    def test_a_non_executable_skill_file_sets_nothing(self):
        p = self._install_skill()
        os.chmod(p.parent / "scripts" / "route_handler.py", 0o644)
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        _, _, env = self.h.watcher()
        self.assertNotIn("SUTANDO_TASK_EVENT_HANDLER=", env)


class WorkerLaunchStartsNoNotifier(unittest.TestCase):
    def test_worker_instance_gets_no_watcher_session(self):
        h = Harness()
        try:
            wid = "c" * 32
            run = h.launch(extra_env={"SUTANDO_INSTANCE_ID": wid,
                                      "SUTANDO_TMUX_SESSION": "sutando-worker-" + wid,
                                      "SUTANDO_TASKS_DIR": "/tmp/never-read-worker-inbox",
                                      "SUTANDO_CLAUDE_SESSION_ID": "11111111-2222-3333-4444-555555555555"})
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            sessions = h.tm("list-sessions", "-F", "#{session_name}").stdout.split()
            self.assertFalse(any(s.endswith("-watcher") for s in sessions), sessions)
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
