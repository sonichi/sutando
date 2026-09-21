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


class WorkspaceTripleSurvivesAPipeInAPathComponent(unittest.TestCase):
    """keweichen round 20: resolve_effective_workspace_triple() used to
    `|`-join (workspace, tasks, results) -- lossy, since `|` is a legal path
    character. Real launcher, real tmux: the exact value forwarded to the
    watcher must match what was requested, unmangled."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_a_pipe_in_the_workspace_reaches_the_watcher_unmangled(self):
        # realpath: values are canonicalized now, correct on macOS and Linux CI.
        workspace = os.path.realpath("/tmp/collide/a|b")
        tasks = os.path.realpath("/tmp/collide/c")
        results = os.path.realpath("/tmp/collide/d")
        run = self.h.launch(extra_env={
            "SUTANDO_WORKSPACE_DIR": "/tmp/collide/a|b",
            "SUTANDO_TASKS_DIR": "/tmp/collide/c",
            "SUTANDO_RESULTS_DIR": "/tmp/collide/d",
        })
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        _, _, env = self.h.watcher()
        self.assertIn(f"SUTANDO_WORKSPACE_DIR={workspace}", env)
        self.assertIn(f"SUTANDO_TASKS_DIR={tasks}", env)
        self.assertIn(f"SUTANDO_RESULTS_DIR={results}", env)

    def test_two_triples_colliding_under_the_old_pipe_join_get_different_restart_identities(self):
        """Both triples below `|`.join() to the identical string
        "/tmp/collide/a|b|/tmp/collide/c|/tmp/collide/d" -- the exact
        collision this fix must break."""
        run_a = self.h.launch(extra_env={
            "SUTANDO_WORKSPACE_DIR": "/tmp/collide/a|b",
            "SUTANDO_TASKS_DIR": "/tmp/collide/c",
            "SUTANDO_RESULTS_DIR": "/tmp/collide/d",
        })
        self.assertEqual(run_a.returncode, 0, run_a.stdout + run_a.stderr)
        _, _, env_a = self.h.watcher()
        version_a = next(l for l in env_a.splitlines() if l.startswith("SUTANDO_NOTIFIER_VERSION="))
        self.h.close()

        self.h = Harness()
        run_b = self.h.launch(extra_env={
            "SUTANDO_WORKSPACE_DIR": "/tmp/collide/a",
            "SUTANDO_TASKS_DIR": "b|/tmp/collide/c",
            "SUTANDO_RESULTS_DIR": "/tmp/collide/d",
        })
        self.assertEqual(run_b.returncode, 0, run_b.stdout + run_b.stderr)
        _, _, env_b = self.h.watcher()
        version_b = next(l for l in env_b.splitlines() if l.startswith("SUTANDO_NOTIFIER_VERSION="))

        # Isolate "-e<hash>" -- the full version string can differ between two
        # Harness setups for unrelated reasons (a resolved python path, a pane id).
        e_a = version_a.rsplit("-e", 1)[-1]
        e_b = version_b.rsplit("-e", 1)[-1]
        self.assertNotEqual(
            e_a, e_b,
            "two genuinely different (workspace, tasks, results) triples produced the "
            "SAME workspace-triple hash component on the real Claude launcher -- "
            f"the `|` collision is not closed (version_a={version_a!r} version_b={version_b!r})",
        )


class NotifierBootGateRefusesOnSweepFailure(unittest.TestCase):
    """keweichen's review on PR #4503 (round 5+6): the notifier starts its OWN
    watcher independent of core's /startup Step 1.7, so it needs the same
    fail-closed gate -- and "no NEW session" is not sufficient on a reuse
    path: an existing watcher must be killed, not left running unprotected."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def _fake_sweep(self, rc: int) -> Path:
        sweep = self.h.td / "fake-sweep.py"
        sweep.write_text(f"#!/usr/bin/env python3\nimport sys; sys.exit({rc})\n")
        sweep.chmod(0o755)
        return sweep

    def test_a_failing_sweep_starts_the_core_but_no_watcher(self):
        sweep = self._fake_sweep(3)
        run = self.h.launch(extra_env={"SUTANDO_POOL_BOOT_SWEEP": str(sweep)})
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        sessions = self.h.tm("list-sessions", "-F", "#{session_name}").stdout.split()
        self.assertIn("sutando-core", sessions, "the core itself must still start")
        self.assertFalse(any(s.endswith("-watcher") for s in sessions),
                          f"a watcher session started despite a failing boot sweep: {sessions}")
        # The failure must be OBSERVABLE, not inferable only from an absence.
        self.assertIn("FATAL notifier-boot-gate", run.stderr,
                      "a failing sweep produced no explicit fatal diagnostic")
        self.assertIn("WITHOUT a notifier intake path", run.stderr)

    def test_a_healthy_watcher_then_a_failing_sweep_kills_it_not_just_skips_a_replacement(self):
        """The reuse path: a watcher already running (matching version) from
        before the sweep started failing must not be left alive."""
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        exists, _, _ = self.h.watcher()
        self.assertTrue(exists, "precondition: a healthy watcher must exist before the reuse case")

        sweep = self._fake_sweep(1)
        run2 = self.h.launch(extra_env={"SUTANDO_POOL_BOOT_SWEEP": str(sweep)})
        self.assertEqual(run2.returncode, 0, run2.stdout + run2.stderr)

        exists_after, _, _ = self.h.watcher()
        self.assertFalse(exists_after,
                          "the watcher session survived a subsequent failing sweep -- "
                          "'no new session' is not the same as fail-closed")

    def test_a_stale_version_watcher_then_a_failing_sweep_also_kills_it(self):
        """The stale-version path already kills-and-would-restart; a failing
        sweep must still leave it dead, not silently restart the old one."""
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        # Force a version mismatch the way a code/env change would.
        self.h.tm("set-environment", "-t", "=sutando-core-watcher",
                  "SUTANDO_NOTIFIER_VERSION", "stale-version-marker")

        sweep = self._fake_sweep(1)
        run2 = self.h.launch(extra_env={"SUTANDO_POOL_BOOT_SWEEP": str(sweep)})
        self.assertEqual(run2.returncode, 0, run2.stdout + run2.stderr)

        exists_after, _, _ = self.h.watcher()
        self.assertFalse(exists_after,
                          "a stale-version watcher survived a failing sweep instead of being killed")

    def test_a_healthy_sweep_still_starts_the_watcher_normally(self):
        """Negative control: the gate must not be permanently closed -- a
        passing sweep is the ordinary path, unchanged."""
        sweep = self._fake_sweep(0)
        run = self.h.launch(extra_env={"SUTANDO_POOL_BOOT_SWEEP": str(sweep)})
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        exists, _, _ = self.h.watcher()
        self.assertTrue(exists, "a passing sweep must not block the ordinary notifier start")


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
