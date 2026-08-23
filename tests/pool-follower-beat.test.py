#!/usr/bin/env python3
"""Contract tests for wrapper-owned follower liveness (L3 fix).

pool-follower-beat.sh must beat `<ws>/state/cores/<instance>.alive` while the
watched pid is alive, stop when it dies, and never unlink the file — the 90s
stale window is what absorbs KeepAlive restart gaps, so an unlink would make
the lead reclaim in-flight assignments on every clean follower restart.

pool-core-wrapper.sh (persistent form) must create the tmux session when
absent, beat `core-$SUTANDO_CORE_ID` pid-bound to the pane while the session
lives, and exit 0 promptly once the session ends (launchd KeepAlive owns the
restart). Exercised against a stub tmux + stub claude — the live tmux server
must never see test sessions.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BEAT = REPO / "scripts" / "pool-follower-beat.sh"
WRAPPER = REPO / "scripts" / "pool-core-wrapper.sh"


def _wait_for(cond, timeout=10.0, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


class PoolFollowerBeatTest(unittest.TestCase):
    def test_beats_while_watched_alive_then_stops_without_unlink(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            watched = subprocess.Popen(["sleep", "300"])
            env = dict(os.environ, SUTANDO_POOL_BEAT_INTERVAL="1")
            beater = subprocess.Popen(
                ["bash", str(BEAT), "core-7", str(ws), str(watched.pid)],
                env=env)
            alive = ws / "state" / "cores" / "core-7.alive"
            try:
                self.assertTrue(_wait_for(alive.exists),
                                "beat file never appeared")
                payload = json.loads(alive.read_text())
                self.assertEqual(payload["role"], "follower")
                self.assertEqual(payload["instance"], "core-7")
                self.assertEqual(payload["pid"], watched.pid)

                m1 = alive.stat().st_mtime
                self.assertTrue(
                    _wait_for(lambda: alive.stat().st_mtime > m1),
                    "mtime never advanced — a single write is not a beat")

                watched.terminate()
                watched.wait(timeout=10)
                self.assertIsNotNone(
                    beater.wait(timeout=10),
                    "beater must exit once the watched pid is gone")
                self.assertTrue(alive.exists(),
                                "beat file must survive follower exit "
                                "(no unlink — restart-gap contract)")
            finally:
                for p in (watched, beater):
                    if p.poll() is None:
                        p.kill()

    def test_wrapper_beats_core_id_and_exits_when_session_ends(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            stub_claude = ws / "stub-claude"
            stub_claude.write_text("#!/bin/bash\nsleep 3\n")
            stub_claude.chmod(0o755)
            stub_tmux = ws / "stub-tmux"
            stub_tmux.write_text(
                '#!/bin/bash\n'
                'D="$STUB_TMUX_DIR"\n'
                'case "$1" in\n'
                '  has-session) [ -f "$D/session-alive" ];;\n'
                '  new-session)\n'
                '    touch "$D/session-alive"\n'
                '    ( bash -c "${@: -1}"; rm -f "$D/session-alive" ) &\n'
                '    echo $! > "$D/pane-pid";;\n'
                '  list-panes) cat "$D/pane-pid";;\n'
                'esac\n')
            stub_tmux.chmod(0o755)
            env = dict(
                os.environ,
                POOL_REPO_DIR=str(REPO),
                POOL_CLAUDE_BIN=str(stub_claude),
                POOL_TMUX_BIN=str(stub_tmux),
                STUB_TMUX_DIR=str(ws),
                POOL_WORKSPACE=str(ws),
                SUTANDO_CORE_ID="9",
                SUTANDO_POOL_BEAT_INTERVAL="1",
                SUTANDO_POOL_SESSION_POLL="1")
            wrapper = subprocess.Popen(["bash", str(WRAPPER)], env=env)
            alive = ws / "state" / "cores" / "core-9.alive"
            try:
                self.assertTrue(
                    _wait_for(alive.exists),
                    "wrapper never produced core-9.alive while the "
                    "session ran")
                pane_pid = int((ws / "pane-pid").read_text().strip())
                self.assertEqual(
                    json.loads(alive.read_text())["pid"], pane_pid,
                    "beat must be pid-bound to the tmux pane")
                self.assertEqual(
                    wrapper.wait(timeout=15), 0,
                    "wrapper must exit 0 once the session ends "
                    "(KeepAlive owns the restart)")
                self.assertTrue(
                    alive.exists(),
                    "beat file must survive wrapper exit (restart-gap "
                    "contract)")
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()


class PoolWrapperRuntimeDispatchTest(unittest.TestCase):
    """POOL_RUNTIME picks the launch form; an unknown one must not fall back."""

    # Records the command string tmux was asked to run, then lets the session
    # end so the wrapper exits without a live agent ever being started.
    STUB_TMUX = (
        '#!/bin/bash\n'
        'D="$STUB_TMUX_DIR"\n'
        'case "$1" in\n'
        '  has-session) [ -f "$D/session-alive" ];;\n'
        '  new-session)\n'
        '    printf "%s" "${@: -1}" > "$D/launch-cmd"\n'
        '    touch "$D/session-alive"\n'
        '    ( sleep 1; rm -f "$D/session-alive" ) &\n'
        '    echo $! > "$D/pane-pid";;\n'
        '  list-panes) cat "$D/pane-pid";;\n'
        '  *) exit 0;;\n'
        'esac\n')

    def run_wrapper(self, td: Path, **envextra):
        stub_tmux = td / "stub-tmux"
        stub_tmux.write_text(self.STUB_TMUX)
        stub_tmux.chmod(0o755)
        for name in ("stub-claude", "stub-codex"):
            b = td / name
            b.write_text("#!/bin/bash\nsleep 3\n")
            b.chmod(0o755)
        env = dict(
            os.environ,
            POOL_REPO_DIR=str(REPO),
            POOL_TMUX_BIN=str(stub_tmux),
            STUB_TMUX_DIR=str(td),
            POOL_WORKSPACE=str(td),
            SUTANDO_CORE_ID="4",
            SUTANDO_POOL_BEAT_INTERVAL="1",
            SUTANDO_POOL_SESSION_POLL="1")
        env.pop("POOL_RUNTIME", None)
        env.pop("POOL_RUNTIME_BIN", None)
        env.update(envextra)
        r = subprocess.run(["bash", str(WRAPPER)], env=env,
                           capture_output=True, text=True, timeout=60)
        cmd = td / "launch-cmd"
        return r, (cmd.read_text() if cmd.exists() else None)

    def test_codex_runtime_launches_the_codex_form(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, cmd = self.run_wrapper(
                td, POOL_RUNTIME="codex",
                POOL_RUNTIME_BIN=str(td / "stub-codex"),
                POOL_RUNTIME_CONFIG_ENV="CODEX_HOME",
                POOL_RUNTIME_CONFIG_DIR=str(td / "codex-home"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIsNotNone(cmd, "no session was launched")
            self.assertIn(str(td / "stub-codex"), cmd)
            self.assertNotIn("stub-claude", cmd)
            for flag in ("--sandbox danger-full-access",
                         "--ask-for-approval never", "--no-alt-screen"):
                self.assertIn(flag, cmd, f"codex launch is missing {flag}")
            self.assertNotIn("--dangerously-skip-permissions", cmd,
                             "claude's flags must not reach codex")
            self.assertNotIn("/proactive-loop-pool'", cmd,
                             "codex has no slash-command surface")
            self.assertIn("skills/proactive-loop-pool/CODEX.md", cmd,
                          "the codex entry must point at CODEX.md")
            self.assertIn("core-4", cmd, "the entry must name the core")
            self.assertIn(f"CODEX_HOME='{td / 'codex-home'}'", cmd,
                          "the resolved codex store must be forwarded")

    def test_claude_is_the_default_and_an_old_plist_still_works(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            # No POOL_RUNTIME / POOL_RUNTIME_BIN: exactly a plist written before
            # the runtime dimension existed.
            r, cmd = self.run_wrapper(td, POOL_CLAUDE_BIN=str(td / "stub-claude"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(str(td / "stub-claude"), cmd)
            self.assertIn("--dangerously-skip-permissions", cmd)
            self.assertIn("-- '/proactive-loop-pool'", cmd)
            self.assertNotIn("--sandbox", cmd)

    def test_explicit_claude_runtime_launches_the_claude_form(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, cmd = self.run_wrapper(
                td, POOL_RUNTIME="claude",
                POOL_RUNTIME_BIN=str(td / "stub-claude"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("--dangerously-skip-permissions", cmd)
            self.assertIn("-- '/proactive-loop-pool'", cmd)

    def test_unsupported_runtime_exits_2_without_launching(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            r, cmd = self.run_wrapper(
                td, POOL_RUNTIME="gemini",
                POOL_RUNTIME_BIN=str(td / "stub-claude"))
            self.assertEqual(r.returncode, 2,
                             "an unknown runtime must fail loudly, like "
                             "src/agent/start-cli.sh")
            self.assertIsNone(cmd, "no session may start for an unknown runtime")
            self.assertIn("unsupported core runtime", r.stderr)


if __name__ == "__main__":
    unittest.main()
