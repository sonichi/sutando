#!/usr/bin/env python3
"""The watcher keeps its identity across reruns, heals and pane replacement.

Split out of start-cli-claude-task-notifier.test.py by behaviour (that file keeps
the Harness and the stubs) so the suite's cost spreads across CI legs.
Run: python3 tests/start-cli-claude-task-notifier-identity.test.py
"""
from __future__ import annotations

import importlib.util
import subprocess
import time
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "start_cli_claude_task_notifier_harness",
    Path(__file__).resolve().parent / "start-cli-claude-task-notifier.test.py")
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)
# Only the harness and its constants; that file's TestCase classes stay there.
Harness = _h.Harness
TMUX = _h.TMUX


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
