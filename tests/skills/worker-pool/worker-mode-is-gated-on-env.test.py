#!/usr/bin/env python3
"""The pool's two edits to existing scripts are invisible to the core.

`start-cli.sh` gains a worker mode and `watch-tasks-stream.sh` gains a
delivery-folder override; both are gated on env the core never sets. These
tests pin the gate from both sides: unset, the core's launch and the core's
watched folder are what they were; set, the worker's are what the pool needs.

Run: python3 tests/worker-mode-is-gated-on-env.test.py
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


@contextlib.contextmanager
def scratch():
    """A temp dir whose removal tolerates a straggler from the watcher's process
    group. The kwarg that used to buy that is 3.10+, and CONTRIBUTING.md's
    supported floor is 3.9."""
    path = tempfile.mkdtemp()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)

REPO = Path(__file__).resolve().parents[3]

# The launcher polls `pgrep -ax claude`, then `ps -o args=` for `--name $SESSION`:
# the stub reports the launched stub's own pid, and the real ps shows its argv.
PGREP_STUB = ('[ "$*" = "-ax claude" ] || exit 0\n'
              '[ -s "$HOME/claude.pid" ] && echo "$(cat "$HOME/claude.pid") claude"\n')


def _launch_argv(extra_env: dict, pgrep_stub: str = PGREP_STUB) -> list[str]:
    """start-cli.sh through its real tmux path on a private socket, from a COPIED
    repo whose sutando-config.sh names a scratch workspace: past its liveness
    poll the launcher clears the shutdown sentinel and ensures the supervisor,
    and the real workspace must never be a test's write target. Returns the
    argv of the process it put in the pane. A stub claude that persists is what
    keeps the session alive long enough to read it.

    The pgrep stub answers the liveness probe with nothing until the stub claude
    has recorded its pid; every other probe (the monitor guard) still "finds"
    its target, and a live pid in the relay pidfile passes that guard, so no
    launcher child outlives the run.

    Raises AssertionError unless the launcher's own verdict was success (exit 0):
    a pane left behind by a failed launch is not the state under test."""
    import shutil
    tmux = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if not tmux:
        raise unittest.SkipTest("tmux not found")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        root = td / "repo"
        shutil.copytree(REPO / "src", root / "src", symlinks=True)
        shutil.copytree(REPO / "scripts", root / "scripts", symlinks=True)
        ws = td / "workspace"; (ws / "state").mkdir(parents=True)
        (root / "scripts" / "sutando-config.sh").write_text(
            '#!/bin/bash\ncase "$1" in\n'
            '  workspace) echo "%s";;\n'
            '  claude-sutando-config-dir) echo "%s/.claude-sutando";;\n'
            '  python-bin) echo python3;;\n'
            '  core-runtime) echo claude;;\n'
            '  host-label) echo testhost;;\n'
            '  *) echo "";;\nesac\n' % (ws, ws))
        (ws / "state" / "core-supervisor-relay-loop.pid").write_text(str(os.getpid()))
        bind = td / "bin"; bind.mkdir(); (td / "home").mkdir()
        sock = td / "t.sock"
        for stub, body in (("claude", 'echo $$ > "$HOME/claude.pid"\nsleep 300\n'), ("pgrep", pgrep_stub),
                           ("lsof", "exit 1\n"), ("launchctl", "exit 1\n")):
            (bind / stub).write_text("#!/bin/bash\n" + body); (bind / stub).chmod(0o755)
        tm = lambda *a: subprocess.run([tmux, "-S", str(sock), *a], capture_output=True, text=True)
        env = {"PATH": f"{bind}:{Path(tmux).parent}:/usr/bin:/bin:/usr/sbin", "HOME": str(td / "home"),
               "SUTANDO_TMUX_SOCKET": str(sock), "SUTANDO_TEST_MODE": "1", **extra_env}
        try:
            run = subprocess.run(["/bin/bash", str(root / "src" / "agent" / "claude" / "cli" / "start-cli.sh")],
                                 env=env, capture_output=True, text=True, timeout=60)
            assert run.returncode == 0, (
                f"launcher exited {run.returncode}\nstdout: {run.stdout}\nstderr: {run.stderr}")
            # Every session's panes: the core's own, plus any watcher the launcher
            # started beside it. The startup command is the one carrying --name.
            argv = []
            for pid in tm("list-panes", "-s", "-a", "-F", "#{pane_pid}").stdout.split():
                words = subprocess.run(["ps", "-o", "args=", "-p", pid], capture_output=True, text=True).stdout.split()
                if "--name" in words:
                    argv = words
            sessions = tm("list-sessions", "-F", "#{session_name}").stdout.split()
            return argv, sessions
        finally:
            tm("kill-server")


class TestLauncherGate(unittest.TestCase):
    def test_unset_the_core_launch_keeps_its_owner_surfaces(self):
        argv, sessions = _launch_argv({})
        self.assertTrue(argv, "claude was never exec'd")
        self.assertIn("sutando-core-watcher", sessions, "the core launch owns a task-notifier watcher")
        self.assertIn("--remote-control", argv)
        self.assertIn("--chrome", argv)
        self.assertNotIn("--session-id", argv)
        self.assertIn("sutando-core", argv)
        # ps argv is whitespace-split: the boot prompt is its tail; a core never boots as a worker.
        self.assertEqual(argv[-1], "/startup")
        self.assertNotIn("--worker", argv)

    def test_set_the_worker_launch_drops_them_and_binds_its_session(self):
        argv, sessions = _launch_argv({"SUTANDO_INSTANCE_ID": "a" * 32, "SUTANDO_TMUX_SESSION": "sutando-worker-" + "a" * 32,
                             "SUTANDO_TASKS_DIR": "/tmp/never-read", "SUTANDO_CLAUDE_SESSION_ID": "11111111-2222-3333-4444-555555555555"})
        self.assertTrue(argv, "claude was never exec'd")
        self.assertFalse([s for s in sessions if s.endswith("-watcher")],
                         "a worker must launch no owner-only task notifier: " + str(sessions))
        self.assertNotIn("--remote-control", argv)
        self.assertNotIn("--chrome", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(argv[argv.index("--name") + 1], "sutando-worker-" + "a" * 32)
        self.assertNotIn("sutando-core", argv)
        self.assertEqual(argv[-2:], ["/startup", "--worker"])

    def test_control_a_liveness_probe_that_reports_nothing_fails_the_fixture(self):
        """The launcher's poll reads `pgrep -ax claude`; a probe that never names
        the launched process is the launcher's own failure verdict (exit 1), and
        the fixture must surface it rather than assert against the pane it left."""
        with self.assertRaisesRegex(AssertionError, r"launcher exited 1[\s\S]*did not come up"):
            _launch_argv({}, pgrep_stub="exit 0\n")


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


def _state_root(extra_env: dict, td: Path):
    """Run a private watcher on a delivery folder with a stub handler set, so it
    creates its claims dir at boot; report where that dir landed."""
    import signal
    root = td / "repo"
    if not root.exists():
        shutil.copytree(REPO / "src", root / "src", symlinks=True)
        shutil.copytree(REPO / "scripts", root / "scripts", symlinks=True)
    ws = td / "ws"; ws.mkdir(exist_ok=True)
    (root / "scripts" / "sutando-config.sh").write_text('#!/bin/bash\ncase "$1" in workspace) echo "%s";; python-bin) echo python3;; *) echo "";; esac\n' % ws)
    handler = td / "handler.sh"; handler.write_text("#!/bin/bash\nexit 3\n"); handler.chmod(0o755)
    inbox = td / "deliveries" / ("b" * 32)
    env = {**os.environ, "SUTANDO_RESULTS_DIR": str(ws / "results"), "SUTANDO_TASKS_DIR": str(inbox),
           "SUTANDO_TASK_EVENT_HANDLER": str(handler), **extra_env}
    env.pop("SUTANDO_WORKSPACE_DIR", None) if "SUTANDO_WORKSPACE_DIR" not in extra_env else None
    p = subprocess.Popen(["bash", str(root / "src" / "watch-tasks-stream.sh")], cwd=str(root), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    under_ws = ws / "state" / "task-event-handler-claims"
    under_inbox = td / "deliveries" / "state" / "task-event-handler-claims"
    try:
        deadline = time.time() + 6
        while time.time() < deadline and not (under_ws.is_dir() or under_inbox.is_dir()) and p.poll() is None:
            time.sleep(0.1)
        return under_ws.is_dir(), under_inbox.is_dir()
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        time.sleep(0.2)


WORKER_KEYS = ("SUTANDO_INSTANCE_ID", "SUTANDO_TASKS_DIR", "SUTANDO_WORKSPACE_DIR",
               "SUTANDO_INBOX_KIND", "SUTANDO_RESULTS_DIR", "SUTANDO_WORKER_BOOTSTRAP",
               "SUTANDO_INBOX_RESOLVER", "SUTANDO_INBOX_RESOLVER_TIMEOUT",
               "SUTANDO_POOL_DELIVERY_SCRIPT")


def _core_env(extra_env: dict, td: Path) -> list[str]:
    """What the launcher forwards into the core session (its own --print-core-env
    probe), from a clean environment: no pool variable, no proxy, no repo .env."""
    root = td / "repo"
    if not root.exists():
        shutil.copytree(REPO / "src", root / "src", symlinks=True)
        shutil.copytree(REPO / "scripts", root / "scripts", symlinks=True)
    ws = td / "ws"; ws.mkdir(exist_ok=True)
    (root / "scripts" / "sutando-config.sh").write_text('#!/bin/bash\ncase "$1" in workspace) echo "%s";; python-bin) echo python3;; *) echo "";; esac\n' % ws)
    stub = td / "bin"; stub.mkdir(exist_ok=True)
    for name, body in (("lsof", "exit 1\n"), ("launchctl", "exit 0\n"), ("sleep", "exit 0\n")):
        (stub / name).write_text("#!/bin/sh\n" + body); (stub / name).chmod(0o755)
    env = {"HOME": str(td), "PATH": f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin", **extra_env}
    out = subprocess.run(["bash", str(root / "src/agent/claude/cli/start-cli.sh"), "--print-core-env"],
                         cwd=str(root), env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return [tok for tok in out.stdout.split() if "=" in tok]


class TestCoreEnvInvariance(unittest.TestCase):
    """No pool variable set = the core session's env is the pre-pool env: the
    marker is 1 and no worker key is forwarded. Set = the opposite, so the
    first test cannot pass by the probe printing nothing."""

    def test_unset_no_worker_key_reaches_the_core_session(self):
        with scratch() as td:
            env = _core_env({}, Path(td))
        self.assertIn("SUTANDO_CORE_RUNTIME=claude", env, env)   # the probe ran
        self.assertIn("SUTANDO_CORE_SESSION=1", env, env)
        keys = {tok.split("=", 1)[0] for tok in env}
        self.assertFalse(keys & set(WORKER_KEYS), f"worker key forwarded to a core: {keys & set(WORKER_KEYS)}")

    def test_a_worker_is_handed_the_inbox_resolver_and_its_timeout(self):
        """tmux hands a new session the SERVER's env, not this shell's, so a
        resolver the spawner set is absent unless the launcher forwards it —
        and without it the watcher announces the zero-byte sentinel itself."""
        with scratch() as td:
            env = _core_env({"SUTANDO_INSTANCE_ID": "d" * 32,
                             "SUTANDO_INBOX_RESOLVER": "/opt/resolve-inbox",
                             "SUTANDO_INBOX_RESOLVER_TIMEOUT": "7"}, Path(td))
        self.assertIn("SUTANDO_INBOX_RESOLVER=/opt/resolve-inbox", env, env)
        self.assertIn("SUTANDO_INBOX_RESOLVER_TIMEOUT=7", env, env)

    def test_an_unset_resolver_is_not_invented(self):
        """Control: the two keys above are forwarded because they were set, not
        because the launcher names them unconditionally."""
        with scratch() as td:
            env = _core_env({"SUTANDO_INSTANCE_ID": "e" * 32}, Path(td))
        keys = {tok.split("=", 1)[0] for tok in env}
        self.assertNotIn("SUTANDO_INBOX_RESOLVER", keys, env)
        self.assertNotIn("SUTANDO_INBOX_RESOLVER_TIMEOUT", keys, env)

    def test_set_the_marker_is_blanked_and_the_instance_named(self):
        with scratch() as td:
            env = _core_env({"SUTANDO_INSTANCE_ID": "c" * 32}, Path(td))
        self.assertIn("SUTANDO_CORE_SESSION=", env, env)
        self.assertNotIn("SUTANDO_CORE_SESSION=1", env, env)
        self.assertIn("SUTANDO_INSTANCE_ID=" + "c" * 32, env, env)


class TestWatcherGate(unittest.TestCase):
    def test_unset_the_core_watches_its_workspace_tasks_folder(self):
        with scratch() as td:
            core, override = _watched_dir({}, Path(td))
        self.assertTrue(core, "the core's tasks/ was not the watched folder")
        self.assertFalse(override)

    def test_set_a_worker_watches_its_delivery_folder_only(self):
        with scratch() as td:
            core, override = _watched_dir({"SUTANDO_TASKS_DIR": str(Path(td) / "deliveries")}, Path(td))
        self.assertTrue(override, "the delivery folder was not the watched folder")
        self.assertFalse(core, "the worker must not create or watch the core's tasks/")


    def test_an_explicit_workspace_keeps_a_workers_state_out_of_deliveries(self):
        """The spawner names the workspace; the watcher must not infer it from
        the delivery folder it watches."""
        with tempfile.TemporaryDirectory() as td:
            ws_hit, inbox_hit = _state_root({"SUTANDO_WORKSPACE_DIR": str(Path(td) / "ws")}, Path(td))
        self.assertTrue(ws_hit, "claims dir was not created under the explicit workspace")
        self.assertFalse(inbox_hit, "claims dir leaked under deliveries/")

    def test_a_worker_is_handed_the_pool_writer(self):
        """The spawner names the writer; tmux hands the session the SERVER's env, so
        it reaches the worker only if the launcher forwards it -- and without it no
        delivery can ever read `finished`."""
        with scratch() as td:
            env = _core_env({"SUTANDO_INSTANCE_ID": "f" * 32,
                             "SUTANDO_POOL_DELIVERY_SCRIPT": "/opt/pool_delivery.py"}, Path(td))
        self.assertIn("SUTANDO_POOL_DELIVERY_SCRIPT=/opt/pool_delivery.py", env, env)

    def test_an_unset_pool_writer_is_not_invented(self):
        """Control: a host with no pool never sees the key."""
        with scratch() as td:
            env = _core_env({"SUTANDO_INSTANCE_ID": "g" * 32}, Path(td))
        keys = {tok.split("=", 1)[0] for tok in env}
        self.assertNotIn("SUTANDO_POOL_DELIVERY_SCRIPT", keys, env)

    def test_without_it_the_inbox_parent_is_taken_as_the_workspace(self):
        """Control: the seam exists — absent the variable, state lands under deliveries/."""
        with tempfile.TemporaryDirectory() as td:
            ws_hit, inbox_hit = _state_root({}, Path(td))
        self.assertTrue(inbox_hit)
        self.assertFalse(ws_hit)

if __name__ == "__main__":
    unittest.main(verbosity=0)
