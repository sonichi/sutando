#!/usr/bin/env python3
"""A compat stub that runpy-execs another module is invisible to the NEW name.

`src/remote-relay-bridge.py` is a deprecated one-release stub. Its own docstring
states the mechanism and the intent:

    Runs the renamed client IN-PROCESS (runpy) so `pgrep -f remote-relay-bridge`
    liveness checks still match.

In-process is the whole point — and it means the process argv keeps the OLD
filename while executing the NEW code. Every pattern written against the new
name therefore has a blind spot for stub-launched instances:

    pkill -f 'remote-gateway-bridge'  vs  argv 'python3 .../remote-relay-bridge.py'
      -> no match

`src/restart.sh` had that gap twice, and the second one is worse than the first:

  1. the `pkill` list could not kill a stub-launched bridge, and
  2. `STOP_PATTERNS` could not SEE it either — so restart would report every
     service stopped while one kept running.

Two opposite states producing identical output is a broken reporter, not a
near-miss. Measured on a peer host 2026-08-03: a stub-launched instance had been
up **39 days**, survived every restart, and went on writing tasks from 39-day-old
code that predated the tierMap — stamping `owner` unconditionally. The operator
restarted repeatedly and every restart hit the wrong process.

This host is NOT exposed (verified: nothing launches the stub here, and no such
process is running), so this is a portability fix for the shipped script, driven
by a measured incident elsewhere.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESTART = REPO / "src" / "restart.sh"
STUB = REPO / "src" / "remote-relay-bridge.py"

DEPRECATED = "remote-relay-bridge"
CURRENT = "remote-gateway-bridge"

STUB_ARGV = "/opt/homebrew/bin/python3 /Users/x/sutando/src/remote-relay-bridge.py"


class TheStubKeepsTheOldArgv(unittest.TestCase):
    """The premise. If this ever stops being true the rest is unnecessary."""

    def test_stub_execs_the_new_module_in_process(self):
        if not STUB.is_file():
            self.skipTest("compat stub already removed — this fix can go too")
        src = STUB.read_text()
        self.assertIn("runpy", src, "the stub must still be an in-process exec")
        self.assertIn(CURRENT, src, "the stub must still target the renamed module")

    def test_the_current_name_does_not_match_a_stub_launched_argv(self):
        """The control: this is WHY the deprecated pattern is needed. If this
        assertion ever fails, the blind spot is gone and so is the reason."""
        self.assertIsNone(re.search(CURRENT, STUB_ARGV))
        self.assertIsNotNone(re.search(DEPRECATED, STUB_ARGV))


class RestartCoversBothNames(unittest.TestCase):
    def setUp(self):
        self.assertTrue(RESTART.is_file(), f"not found: {RESTART}")
        self.text = RESTART.read_text()

    def test_pkill_targets_the_deprecated_name(self):
        """THE pin — fails on the parent commit."""
        self.assertRegex(
            self.text, rf'pkill -f "{DEPRECATED}"',
            "restart.sh must kill stub-launched bridges; the current-name pkill "
            "cannot match their argv",
        )

    def test_stop_patterns_include_the_deprecated_name(self):
        """The sharper half: without this, restart VERIFIES a still-running
        process as stopped."""
        m = re.search(r"STOP_PATTERNS=\((.*?)\)", self.text, re.S)
        self.assertIsNotNone(m, "STOP_PATTERNS block not found")
        self.assertIn(DEPRECATED, m.group(1),
                      "the stop-verification list must be able to SEE a "
                      "stub-launched bridge, or it reports a false all-stopped")

    def test_the_current_name_is_still_covered(self):
        """Guard against 'fixing' this by swapping one name for the other."""
        self.assertRegex(self.text, rf'pkill -f "{CURRENT}"')
        m = re.search(r"STOP_PATTERNS=\((.*?)\)", self.text, re.S)
        self.assertIn(CURRENT, m.group(1))

    def test_the_reason_is_written_down_next_to_the_pattern(self):
        """A bare extra pattern reads like duplication and gets 'cleaned up'."""
        self.assertIn("runpy", self.text,
                      "state the in-process-exec mechanism, or the next reader "
                      "deletes the deprecated pattern as redundant")


if __name__ == "__main__":
    unittest.main()
