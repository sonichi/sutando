#!/usr/bin/env python3
"""Contract tests for wrapper-owned follower liveness (L3 fix).

pool-follower-beat.sh must beat `<ws>/state/cores/<instance>.alive` while the
watched pid is alive, stop when it dies, and never unlink the file — the 90s
stale window is what absorbs KeepAlive restart gaps, so an unlink would make
the lead reclaim in-flight assignments on every clean follower restart.

pool-worker-wrapper.sh (persistent form) must create the tmux session when
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
WRAPPER = REPO / "scripts" / "pool-worker-wrapper.sh"


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
                ["bash", str(BEAT), "worker-7", str(ws), str(watched.pid)],
                env=env)
            alive = ws / "state" / "cores" / "worker-7.alive"
            try:
                self.assertTrue(_wait_for(alive.exists),
                                "beat file never appeared")
                payload = json.loads(alive.read_text())
                self.assertEqual(payload["role"], "follower")
                self.assertEqual(payload["instance"], "worker-7")
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

    def test_wrapper_beats_worker_id_and_exits_when_session_ends(self):
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
            alive = ws / "state" / "cores" / "worker-9.alive"
            try:
                self.assertTrue(
                    _wait_for(alive.exists),
                    "wrapper never produced worker-9.alive while the "
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
            self.assertIn("worker-4", cmd, "the entry must name the core")
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
            self.assertIn("unsupported worker runtime", r.stderr)


class PoolWrapperNudgeTest(unittest.TestCase):
    """The sweep nudge is delegated, and claude's keystrokes are unchanged."""

    STUB_TMUX = (
        '#!/bin/bash\n'
        'D="$STUB_TMUX_DIR"\n'
        'for a in "$@"; do printf "%s\\n" "$a" >> "$D/argv"; done\n'
        'printf "@@ENDCALL@@\\n" >> "$D/argv"\n'
        'if [ "$1" = "has-session" ]; then [ -f "$D/session-alive" ]; exit $?; fi\n'
        'case "$1" in\n'
        '  new-session)\n'
        '    touch "$D/session-alive"\n'
        '    ( sleep 3; rm -f "$D/session-alive" ) &\n'
        '    echo $! > "$D/pane-pid";;\n'
        '  list-panes) cat "$D/pane-pid";;\n'
        '  capture-pane)\n'
        '    if [ -n "${STUB_BUSY_CAPTURES:-}" ]; then\n'
        '      n=$(cat "$D/capture-count" 2>/dev/null || echo 0)\n'
        '      n=$((n + 1)); printf "%s" "$n" > "$D/capture-count"\n'
        '      if [ "$n" -le "$STUB_BUSY_CAPTURES" ]; then\n'
        '        echo "Working (5s • esc to interrupt)"; exit 0\n'
        '      fi\n'
        '    fi\n'
        '    cat "$D/pane" 2>/dev/null;;\n'
        'esac\n'
        'exit 0\n')

    def run_wrapper(self, td: Path, pane: str, **envextra):
        stub_tmux = td / "stub-tmux"
        stub_tmux.write_text(self.STUB_TMUX)
        stub_tmux.chmod(0o755)
        (td / "pane").write_text(pane)
        for name in ("stub-claude", "stub-codex"):
            b = td / name
            b.write_text("#!/bin/bash\nsleep 5\n")
            b.chmod(0o755)
        env = dict(
            os.environ,
            POOL_REPO_DIR=str(REPO),
            POOL_TMUX_BIN=str(stub_tmux),
            STUB_TMUX_DIR=str(td),
            POOL_WORKSPACE=str(td),
            SUTANDO_CORE_ID="4",
            SUTANDO_POOL_BEAT_INTERVAL="5",
            SUTANDO_POOL_SESSION_POLL="1",
            SUTANDO_POOL_SWEEP_NUDGE_S="0")
        env.pop("POOL_RUNTIME", None)
        env.pop("POOL_RUNTIME_BIN", None)
        env.update(envextra)
        subprocess.run(["bash", str(WRAPPER)], env=env,
                       capture_output=True, text=True, timeout=90)
        calls, cur = [], []
        for line in (td / "argv").read_text().split("\n"):
            if line == "@@ENDCALL@@":
                calls.append(cur)
                cur = []
            else:
                cur.append(line)
        return [c for c in calls if c and c[0] == "send-keys"]

    def test_claude_sweep_keystrokes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            sends = self.run_wrapper(td, "", POOL_CLAUDE_BIN=str(td / "stub-claude"))
            self.assertTrue(sends, "the wrapper never swept")
            for c in sends:
                self.assertEqual(
                    c, ["send-keys", "-t", "worker-4",
                        "/proactive-loop-pool pass", "Enter"])

    def test_codex_sweep_defers_while_the_session_is_busy(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            sends = self.run_wrapper(
                td, "Working (5s • esc to interrupt)\n",
                POOL_RUNTIME="codex", POOL_RUNTIME_BIN=str(td / "stub-codex"))
            self.assertEqual(sends, [],
                             "typing into a running codex turn interleaves")

    def test_codex_assignment_waits_for_idle_then_wakes_exactly_once(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            tasks = td / "tasks"
            tasks.mkdir()
            (tasks / "task-wake.assigned-worker-4.txt").write_text("assigned\n")
            sends = self.run_wrapper(
                td, self.IDLE,
                POOL_RUNTIME="codex",
                POOL_RUNTIME_BIN=str(td / "stub-codex"),
                SUTANDO_POOL_SWEEP_NUDGE_S="9999",
                SUTANDO_POOL_SESSION_POLL="0.1",
                STUB_BUSY_CAPTURES="2")
            self.assertGreaterEqual(
                int((td / "capture-count").read_text()), 3,
                "the assignment wake must stay pending until Codex is idle")
            self.assertEqual(
                len(sends), 2,
                "one durable assignment must produce one literal send + submit")
            self.assertIn("skills/proactive-loop-pool/CODEX.md", sends[0][5])
            self.assertEqual(sends[1],
                             ["send-keys", "-t", "worker-4", "C-m"])

    # The wrapper path drives codex every 300s. It must reach the SAME verdict
    # as the kick path, which classifies each of these panes positively.
    ESC = "\033"
    IDLE = f"{ESC}[1m\u203a{ESC}[0m \n"          # true ANSI idle prompt
    STAGED = f"{ESC}[1m\u203a{ESC}[0m ready\n"   # user text already staged
    DIALOG = "Update available\nPress enter to continue\n"
    GARBLED = "\u2588\u2588 \n"                 # unparseable capture

    def _codex_sends(self, td, pane):
        return self.run_wrapper(
            td, pane, POOL_RUNTIME="codex",
            POOL_RUNTIME_BIN=str(td / "stub-codex"))

    def test_codex_sweep_types_the_codex_entry_only_when_truly_idle(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            sends = self._codex_sends(td, self.IDLE)
            self.assertTrue(sends, "an idle ANSI prompt must receive the entry")
            self.assertEqual(sends[0][:5],
                             ["send-keys", "-t", "worker-4", "-l", "--"])
            self.assertIn("skills/proactive-loop-pool/CODEX.md", sends[0][5])
            self.assertEqual(sends[1], ["send-keys", "-t", "worker-4", "C-m"])

    def test_codex_sweep_never_types_into_a_pane_it_did_not_recognize(self):
        """This test previously asserted the OPPOSITE for the staged case: it fed
        '> ready' and expected a send, encoding the fail-open rather than catching
        it. Each pane below is one the kick path already refuses."""
        for label, pane in (("staged user text", self.STAGED),
                            ("startup dialog", self.DIALOG),
                            ("garbled capture", self.GARBLED),
                            ("empty pane", "")):
            with self.subTest(pane=label), tempfile.TemporaryDirectory() as t:
                sends = self._codex_sends(Path(t), pane)
                self.assertEqual(sends, [],
                                 f"typed into {label}: {sends}")


if __name__ == "__main__":
    unittest.main()
