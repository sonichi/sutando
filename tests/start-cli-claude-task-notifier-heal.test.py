#!/usr/bin/env python3
"""The heal path starts the notifier only once the healed core is proven alive.

Split out of start-cli-claude-task-notifier.test.py by behaviour (that file keeps
the Harness and the stubs) so the suite's cost spreads across CI legs.
Run: python3 tests/start-cli-claude-task-notifier-heal.test.py
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
