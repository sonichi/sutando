#!/usr/bin/env python3
"""restart.sh must relaunch Sutando.app it killed, before its terminal exec.

`restart.sh:73` pkills `src/Sutando/Sutando` and nothing brought it back, so
every restart silently dropped the owner's hotkeys, menu bar, and the app's
`checkWatcher()` timer -- the watchdog that re-arms a missing task watcher.

The relaunch lives in restart.sh, NOT startup.sh: `tests/startup-headless.test.sh`
guards startup.sh as headless -- desktop process management belongs to product
entry points, never the open-source core startup. An earlier revision of this PR
put it in startup.sh and that guard failed it, correctly.

Two assertions are load-bearing beyond "a launch exists":

* **`pgrep -x`, not `-f`.** `pgrep -f Sutando` matches the argv of the shell
  running the check, so it reports the app as running when it is not.
* **The launch must precede the final `exec`.** restart.sh ends in
  `exec bash .../startup.sh`, which replaces the process; anything after it is
  unreachable, yet greps as present.
"""
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

        This is the assertion a source-grep test usually misses: presence is not
        reachability in a script whose last statement replaces the process.
        """
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

    def test_startup_sh_is_still_syntactically_valid(self):
        """The edit is in shell, so parse it rather than trusting the diff."""
        r = subprocess.run(["bash", "-n", str(STARTUP)],
                           capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
