#!/usr/bin/env python3
"""restart.sh --rebuild-app rebuilds the menu-bar app before relaunching it.

Without it a restart relaunches whatever binary is on disk, so a stale build
survives every restart and health-check keeps saying "rebuild needed" with no
command that does it. Set SUTANDO_TEST_RESTART_SH to run these against another
copy (the control run against the parent fails every test below)."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESTART = Path(os.environ.get("SUTANDO_TEST_RESTART_SH") or REPO / "src" / "restart.sh")
BLOCK_RE = re.compile(r'^if \[ "\$REBUILD_APP" -eq 1 \]; then\n.*?^fi\n', re.M | re.S)


class RestartRebuildApp(unittest.TestCase):

    def setUp(self):
        self.text = RESTART.read_text()

    def test_the_flag_is_documented_in_the_usage_header(self):
        self.assertRegex(self.text, re.compile(r"^#   --rebuild-app ", re.M), "usage header does not list --rebuild-app")

    def test_the_flag_arms_the_rebuild(self):
        self.assertRegex(self.text, r'\[ "\$\{1:-\}" = "--rebuild-app" \] && REBUILD_APP=1')

    def test_the_build_runs_after_the_stop_and_before_the_relaunch(self):
        """Building while the old app still runs replaces a mapped binary; building
        after the exec never runs at all."""
        build = self.text.find('bash "$REPO/scripts/install-menu-bar-app.sh"')
        self.assertGreater(build, 0, "restart.sh never calls the install script")
        stop_drained = self.text.index("_shutdown_state clear")
        launch = self.text.index('nohup "$APP_BIN"')
        execs = [m.start() for m in re.finditer(r'^exec bash "\$REPO/src/startup\.sh"', self.text, re.M)]
        self.assertGreater(build, stop_drained, "the build sits before the stop has drained")
        self.assertLess(build, launch, "the build sits after the relaunch, so the relaunch runs the old binary")
        self.assertLess(build, min(execs), "the build sits after the terminal exec and can never run")

    def _run_block(self, rebuild: int, stub_rc: int):
        m = BLOCK_RE.search(self.text)
        self.assertIsNotNone(m, "could not find the rebuild block to exercise")
        d = Path(tempfile.mkdtemp())
        (d / "scripts").mkdir()
        marker = d / "built.txt"
        stub = d / "scripts" / "install-menu-bar-app.sh"
        stub.write_text(f"#!/bin/bash\necho built > {marker}\nexit {stub_rc}\n")
        stub.chmod(0o755)
        block = m.group(0).replace("/tmp/sutando-app-build.log", str(d / "build.log"))
        script = textwrap.dedent(f"""
            REPO={str(d)!r}
            REBUILD_APP={rebuild}
        """) + block + "\necho after-block\n"
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
        return r, marker

    def test_without_the_flag_nothing_is_built(self):
        r, marker = self._run_block(rebuild=0, stub_rc=0)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertFalse(marker.exists(), "the install script ran without --rebuild-app")

    def test_with_the_flag_the_install_script_runs(self):
        r, marker = self._run_block(rebuild=1, stub_rc=0)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertTrue(marker.exists(), "the install script never ran")
        self.assertIn("menu-bar app rebuilt", r.stdout)

    def test_a_failed_build_keeps_the_restart_going(self):
        """A build error must not strand the restart before startup.sh."""
        r, marker = self._run_block(rebuild=1, stub_rc=1)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertTrue(marker.exists())
        self.assertIn("rebuild failed", r.stdout)
        self.assertIn("after-block", r.stdout, "the block aborted the script on a failed build")

    def test_the_script_is_syntactically_valid(self):
        r = subprocess.run(["bash", "-n", str(RESTART)], capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stderr)


if __name__ == "__main__":
    unittest.main()
