#!/usr/bin/env python3
"""restart.sh must relaunch the app it kills at :73, before its terminal exec.
The relaunch lives here, not startup.sh: tests/startup-headless.test.sh guards that file as headless."""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STARTUP = REPO / "src" / "startup.sh"
RESTART = REPO / "src" / "restart.sh"


class RestartRelaunchesTheApp(unittest.TestCase):

    def setUp(self):
        self.text = RESTART.read_text()

    def test_startup_launches_the_app_binary(self):
        """FAILS on the parent, which never references the binary as a launch."""
        self.assertIn("src/Sutando/Sutando", self.text,
                      "restart.sh does not reference the app binary at all")
        self.assertRegex(
            self.text, r'nohup\s+"\$APP_BIN"',
            "no nohup launch of the app binary in restart.sh")

    def test_liveness_uses_pgrep_x_not_dash_f(self):
        """-f self-matches the checking shell and reports a false positive."""
        self.assertIn("pgrep -x Sutando", self.text)
        self.assertNotRegex(
            self.text, r'pgrep -f ["\']?[^"\'\n]*src/Sutando/Sutando',
            "app liveness must not use pgrep -f — it matches this script's argv")

    def test_the_launch_is_reachable_before_the_terminal_exec(self):
        """A block after `exec` greps as present and never runs.
        Presence is not reachability when the last statement replaces the process."""
        launch = self.text.index('nohup "$APP_BIN"')
        execs = [m.start() for m in
                 re.finditer(r'^exec bash "\$REPO/src/startup\.sh"',
                             self.text, re.M)]
        self.assertTrue(execs, "restart.sh no longer ends in the startup.sh exec")
        self.assertLess(launch, min(execs),
                        "the app launch sits AFTER restart.sh's terminal exec, "
                        "so it can never run")

    def test_guarded_against_double_launch(self):
        """Two app instances mean two checkWatcher timers re-arming one watcher."""
        head = self.text[:self.text.index('nohup "$APP_BIN"')]
        self.assertIn("pgrep -x Sutando", head,
                      "the launch is not preceded by an already-running guard")

    def test_missing_binary_skips_instead_of_failing(self):
        """A clone without a built binary must not break restart."""
        self.assertRegex(self.text, r'elif \[ -x "\$APP_BIN" \]')
        self.assertIn("Sutando.app skipped", self.text)

    def test_the_premise_still_holds_restart_kills_it(self):
        """CONTROL: if restart.sh stops killing the app, this fix is moot and the
        test should say so rather than passing for a stale reason."""
        self.assertRegex(RESTART.read_text(),
                         r'pkill -f "src/Sutando/Sutando"',
                         "restart.sh no longer pkills the app — re-evaluate #2810")

    def test_both_edited_scripts_are_syntactically_valid(self):
        """RESTART carries the functional edit; STARTUP had the block removed.
        Parsing only the untouched one lets malformed syntax pass its own test."""
        for script in (RESTART, STARTUP):
            r = subprocess.run(["bash", "-n", str(script)],
                               capture_output=True, text=True)
            self.assertEqual(0, r.returncode, f"{script.name}: {r.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
