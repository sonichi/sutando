#!/usr/bin/env python3
"""restart.sh refuses an unrecognized argument instead of silently running its
default (destructive) action. Set SUTANDO_TEST_RESTART_SH to run these against
another copy (the control run against the parent fails every test below)."""
from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESTART = Path(os.environ.get("SUTANDO_TEST_RESTART_SH") or REPO / "src" / "restart.sh")


class RestartArgvValidation(unittest.TestCase):

    def setUp(self):
        self.text = RESTART.read_text()

    def _run(self, *args):
        return subprocess.run(
            ["bash", str(RESTART), *args],
            capture_output=True, text=True, timeout=10,
        )

    def test_the_validation_case_runs_before_any_destructive_line(self):
        """Every early-exiting case arm must sit before the script's first
        service-affecting command, or an unknown flag still reaches it."""
        case_pos = self.text.index('case "${1:-}" in')
        first_pkill = self.text.index("pkill -f")
        self.assertLess(case_pos, first_pkill, "argv validation sits after a pkill")

    def test_no_args_is_in_the_accepted_pattern(self):
        # A live no-arg run performs the actual restart, so this is checked
        # structurally rather than by invoking the script.
        self.assertIn('""|--stop-only|--rebuild-app', self.text)

    def test_unrecognized_argument_is_refused_before_anything_runs(self):
        r = self._run("--bogus")
        self.assertEqual(2, r.returncode, r.stdout + r.stderr)
        self.assertIn("unrecognized argument", r.stderr)
        self.assertIn("--bogus", r.stderr)

    def test_help_prints_usage_and_exits_clean(self):
        r = self._run("--help")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("Usage: bash src/restart.sh", r.stdout)
        self.assertIn("--stop-only", r.stdout)
        self.assertIn("--rebuild-app", r.stdout)

    def test_short_help_flag_also_works(self):
        r = self._run("-h")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("Usage: bash src/restart.sh", r.stdout)

    def test_a_typo_of_a_real_flag_is_refused_not_silently_run(self):
        """The exact failure mode from the incident this closes: a near-miss on
        a real flag must not fall through to the default action."""
        r = self._run("--stop-onl")
        self.assertEqual(2, r.returncode, r.stdout + r.stderr)
        self.assertIn("unrecognized argument", r.stderr)

    def test_stop_only_and_rebuild_app_are_unaffected(self):
        """The two real flags must still reach the case's pass-through arm,
        not the refusal arm. Checked structurally (both live inside the
        accepted-values pattern), not by a live run, which would actually
        act."""
        m = re.search(r'case "\$\{1:-\}" in\n(.*?)\nesac', self.text, re.S)
        self.assertIsNotNone(m, "could not find the validation case block")
        accepted_arm = m.group(1).splitlines()[0]
        self.assertIn("--stop-only", accepted_arm)
        self.assertIn("--rebuild-app", accepted_arm)

    def test_the_script_is_syntactically_valid(self):
        r = subprocess.run(["bash", "-n", str(RESTART)], capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stderr)


if __name__ == "__main__":
    unittest.main()
