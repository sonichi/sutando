#!/usr/bin/env python3
"""A changed knob or --restart replaces the core's watcher; the Mod picks the standby
mode, only a genuine pin reaches the watcher, and a worker launch starts no notifier.

Split out of start-cli-claude-task-notifier.test.py by behaviour (that file keeps
the Harness and the stubs) so the suite's cost spreads across CI legs.
Run: python3 tests/start-cli-claude-task-notifier-modes.test.py
"""
from __future__ import annotations

import importlib.util
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
REPO = _h.REPO


class CoreLaunchReplacesTheWatcherOnChange(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def tearDown(self):
        self.h.close()

    def test_custom_alert_knobs_reach_the_watcher_and_a_change_replaces_it(self):
        created_before = self.h.watcher_created()
        time.sleep(1.1)   # session_created has 1 s resolution
        run = self.h.launch(extra_env={"SUTANDO_NOTIFIER_ALERT_GRACE": "60", "SUTANDO_NOTIFIER_ALERT_REPEAT": "0"})
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        _, _, env = self.h.watcher()
        self.assertIn("SUTANDO_NOTIFIER_ALERT_GRACE=60", env.splitlines())
        self.assertIn("SUTANDO_NOTIFIER_ALERT_REPEAT=0", env.splitlines())
        self.assertNotEqual(created_before, self.h.watcher_created(), "a changed knob replaces the supervisor")

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


class CoreStandbyModeFollowsTheTaskWatchMod(unittest.TestCase):
    """With the task-watch Mod installed the core's supervisor is alert-only (never types);
    without it the typing standby stays; switching the Mod replaces the supervisor."""

    def setUp(self):
        # A claude that passes the Mod's version floor, so the launcher installs the Mod.
        self.h = Harness(claude_body='[ "$1" = --version ] && { echo 99.0.0; exit 0; }\n'
                                     'echo $$ > "$HOME/claude.pid"\nsleep 120\n')

    def tearDown(self):
        self.h.close()

    def mode(self):
        _, _, env = self.h.watcher()
        return [l for l in env.splitlines() if l.startswith("SUTANDO_STANDBY_MODE=")]

    def test_mod_on_gives_the_alert_only_standby_and_mod_off_the_typing_one(self):
        run = self.h.launch(extra_env={"SUTANDO_TASK_WATCH_MOD_CORE": "1"})
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.mode(), ["SUTANDO_STANDBY_MODE=alert"], run.stderr)
        created_before = self.h.watcher_created()
        time.sleep(1.1)   # session_created has 1 s resolution
        run = self.h.launch(extra_env={"SUTANDO_TASK_WATCH_MOD_CORE": "0"})
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.mode(), ["SUTANDO_STANDBY_MODE=type"])
        self.assertNotEqual(created_before, self.h.watcher_created(), "switching the Mod replaces the supervisor")


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
