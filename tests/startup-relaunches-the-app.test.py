#!/usr/bin/env python3
"""startup.sh must relaunch Sutando.app, before its terminal exec.

Run:
    python3 tests/startup-relaunches-the-app.test.py

`restart.sh:73` pkills `src/Sutando/Sutando`; `startup.sh` — which restart.sh
execs to bring services back — never started it, and no launchd job covers it
(#2810). So every restart silently dropped the owner's hotkeys, the menu bar,
and the app's `checkWatcher()` timer, which is the watchdog that re-arms a
missing task watcher. Measured 2026-08-11: the app stayed down ~3 hours after a
restart, reported only as ordinary staleness.

Two assertions are load-bearing beyond "a launch exists":

* **`pgrep -x`, not `-f`.** `pgrep -f Sutando` matches the argv of the shell
  running the check, so it reports the app as running when it is not — the
  liveness probe would then never launch anything.
* **The launch must precede the final `exec`.** startup.sh ends in
  `exec bash .../start-cli.sh`, which replaces the process; anything after it
  is unreachable. A block appended below that line looks correct in a diff,
  greps as present, and never runs once.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STARTUP = REPO / "src" / "startup.sh"
RESTART = REPO / "src" / "restart.sh"


class StartupRelaunchesTheApp(unittest.TestCase):

    def setUp(self):
        self.text = STARTUP.read_text()

    def test_startup_launches_the_app_binary(self):
        """FAILS on the parent, which never references the binary as a launch."""
        self.assertIn("src/Sutando/Sutando", self.text,
                      "startup.sh does not reference the app binary at all")
        self.assertRegex(
            self.text, r'nohup\s+"\$APP_BIN"',
            "no nohup launch of the app binary in startup.sh")

    def test_liveness_uses_pgrep_x_not_dash_f(self):
        """-f self-matches the checking shell and reports a false positive."""
        self.assertIn("pgrep -x Sutando", self.text)
        self.assertNotRegex(
            self.text, r'pgrep -f ["\']?[^"\'\n]*src/Sutando/Sutando',
            "app liveness must not use pgrep -f — it matches this script's argv")

    def test_the_launch_is_reachable_before_the_terminal_exec(self):
        """A block after `exec` greps as present and never runs.

        This is the assertion a source-grep test usually misses: presence is not
        reachability in a script whose last statement replaces the process.
        """
        launch = self.text.index('nohup "$APP_BIN"')
        execs = [m.start() for m in
                 re.finditer(r'^exec bash "\$REPO/src/agent/start-cli\.sh"',
                             self.text, re.M)]
        self.assertTrue(execs, "startup.sh no longer ends in the start-cli exec")
        self.assertLess(launch, min(execs),
                        "the app launch sits AFTER startup.sh's terminal exec, "
                        "so it can never run")

    def test_guarded_against_double_launch(self):
        """Two app instances mean two checkWatcher timers re-arming one watcher."""
        head = self.text[:self.text.index('nohup "$APP_BIN"')]
        self.assertIn("pgrep -x Sutando", head,
                      "the launch is not preceded by an already-running guard")

    def test_missing_binary_skips_instead_of_failing(self):
        """A clone without a built binary must not break startup."""
        self.assertRegex(self.text, r'elif \[ -x "\$APP_BIN" \]')
        self.assertIn("Sutando.app skipped", self.text)

    def test_the_premise_still_holds_restart_kills_it(self):
        """CONTROL: if restart.sh stops killing the app, this fix is moot and the
        test should say so rather than passing for a stale reason."""
        self.assertRegex(RESTART.read_text(),
                         r'pkill -f "src/Sutando/Sutando"',
                         "restart.sh no longer pkills the app — re-evaluate #2810")

    def test_startup_sh_is_still_syntactically_valid(self):
        """The edit is in shell, so parse it rather than trusting the diff."""
        r = subprocess.run(["bash", "-n", str(STARTUP)],
                           capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
