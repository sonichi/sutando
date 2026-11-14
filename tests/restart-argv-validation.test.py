#!/usr/bin/env python3
"""restart.sh refuses an unrecognized argument instead of silently running its
default (destructive) action. Set SUTANDO_TEST_RESTART_SH to run these against
another copy (the control run against the parent fails every test below).

Behavioral tests run the extracted validation BLOCK in a bare subshell, never
the real script — the block's own default arm is `;;` (a no-op), so even a
broken extraction can't reach a pkill. Structural tests just read the source."""
from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESTART = Path(os.environ.get("SUTANDO_TEST_RESTART_SH") or REPO / "src" / "restart.sh")
BLOCK_RE = re.compile(
    r'^if \[ "\$#" -gt \d+ \]; then\n.*?^esac\n', re.M | re.S)


class RestartArgvValidation(unittest.TestCase):

    def setUp(self):
        self.text = RESTART.read_text()

    def _run_block(self, *args):
        """Run ONLY the extracted validation block, standing in for the whole
        script — never the real restart.sh, so a broken guard can't reach the
        real pkill lines below it."""
        m = BLOCK_RE.search(self.text)
        self.assertIsNotNone(m, "could not find the validation block")
        script = m.group(0) + 'echo "past-guard"\n'
        # $0 must be the real restart.sh path: the block's --help/-h arm reads
        # its own usage header via `sed ... "$0"`, exactly as it does live.
        return subprocess.run(
            ["bash", "-c", script, str(RESTART), *args],
            capture_output=True, text=True, timeout=10,
        )

    def test_the_validation_block_runs_before_any_destructive_line(self):
        """The whole block (multi-arg check + case) must sit before the
        script's first side-effecting command -- _shutdown_state mark writes
        a sentinel file, which is the true first effect, earlier than any
        pkill (a prose mention of 'pkill -f' in a comment sits earlier still
        and would pass this check without proving anything)."""
        block_pos = self.text.index('if [ "$#" -gt 1 ]; then')
        first_side_effect = self.text.index('_shutdown_state mark')
        self.assertLess(block_pos, first_side_effect,
                         "argv validation sits after the first side effect")

    def test_no_args_passes_through(self):
        r = self._run_block()
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertIn("past-guard", r.stdout)

    def test_stop_only_and_rebuild_app_pass_through(self):
        for flag in ("--stop-only", "--rebuild-app"):
            r = self._run_block(flag)
            self.assertEqual(0, r.returncode, f"{flag}: {r.stdout + r.stderr}")
            self.assertIn("past-guard", r.stdout, flag)

    def test_unrecognized_argument_is_refused_before_anything_runs(self):
        r = self._run_block("--bogus")
        self.assertEqual(2, r.returncode, r.stdout + r.stderr)
        self.assertIn("unrecognized argument", r.stderr)
        self.assertIn("--bogus", r.stderr)
        self.assertNotIn("past-guard", r.stdout)

    def test_help_prints_usage_and_exits_clean(self):
        r = self._run_block("--help")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("Usage: bash src/restart.sh", r.stdout)
        self.assertIn("--stop-only", r.stdout)
        self.assertIn("--rebuild-app", r.stdout)
        self.assertNotIn("past-guard", r.stdout)

    def test_short_help_flag_also_works(self):
        r = self._run_block("-h")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("Usage: bash src/restart.sh", r.stdout)
        self.assertNotIn("past-guard", r.stdout)

    def test_a_typo_of_a_real_flag_is_refused_not_silently_run(self):
        """The exact failure mode from the incident this closes: a near-miss on
        a real flag must not fall through to the default action."""
        r = self._run_block("--stop-onl")
        self.assertEqual(2, r.returncode, r.stdout + r.stderr)
        self.assertIn("unrecognized argument", r.stderr)
        self.assertNotIn("past-guard", r.stdout)

    def test_a_second_argument_is_refused_even_when_the_first_is_real(self):
        """The gap this test closes: only $1 was ever consulted, so
        `--rebuild-app --stop-only` fell through the case's pass-through arm
        and ran a full restart with a rebuild, silently ignoring --stop-only."""
        r = self._run_block("--rebuild-app", "--stop-only")
        self.assertEqual(2, r.returncode, r.stdout + r.stderr)
        self.assertIn("unrecognized argument", r.stderr)
        self.assertNotIn("past-guard", r.stdout)

    def test_a_real_flag_followed_by_garbage_is_also_refused(self):
        """The other shape of the same gap: `--stop-only --bogus` used to exit
        0 (the case matched $1, the extra arg was never looked at)."""
        r = self._run_block("--stop-only", "--bogus")
        self.assertEqual(2, r.returncode, r.stdout + r.stderr)
        self.assertIn("unrecognized argument", r.stderr)
        self.assertNotIn("past-guard", r.stdout)

    def test_stop_only_and_rebuild_app_are_unaffected(self):
        """The two real flags must still reach the case's pass-through arm,
        checked structurally against the source too (belt-and-suspenders with
        the behavioral test above)."""
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
