#!/usr/bin/env python3
"""Tests for src/watch-tasks-stream.sh — static / grep-level checks.

The shell script requires fswatch (macOS), so we can't run it end-to-end
in CI. These tests verify the static structure of the script (filter logic,
cooldown variables, emit pattern) to catch regressions from merges.
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "watch-tasks-stream.sh"


def _text():
    return SCRIPT.read_text()


class TestScriptExists(unittest.TestCase):
    def test_script_exists(self):
        self.assertTrue(SCRIPT.exists(), f"missing: {SCRIPT}")

    def test_script_is_executable(self):
        import os
        self.assertTrue(os.access(SCRIPT, os.X_OK), "script is not executable")


class TestCooldownPresent(unittest.TestCase):
    """#1070: 3-second cooldown dedup to break rename-loop feedback."""

    def test_last_emit_bn_variable(self):
        self.assertIn("LAST_EMIT_BN", _text())

    def test_last_emit_sec_variable(self):
        self.assertIn("LAST_EMIT_SEC", _text())

    def test_cooldown_duration_is_3s(self):
        self.assertIn("-lt 3", _text())

    def test_continue_on_cooldown(self):
        # The loop must `continue` (not emit) when within the cooldown window.
        self.assertIn("continue", _text())

    def test_emit_updates_last_bn(self):
        # After emitting, LAST_EMIT_BN must be set so next event can dedup.
        text = _text()
        self.assertIn("LAST_EMIT_BN=", text)
        # Must be set to $bn (the basename), not a literal string.
        self.assertIn('LAST_EMIT_BN="$bn"', text)

    def test_emit_updates_last_sec(self):
        text = _text()
        self.assertIn("LAST_EMIT_SEC=$SECONDS", text)


class TestFilters(unittest.TestCase):
    """Existing filter logic must remain intact."""

    def test_parent_dir_filter(self):
        # Only emit for files that are direct children of TASKS_DIR.
        self.assertIn("TASKS_DIR_ABS", _text())
        self.assertIn('dirname "$path"', _text())

    def test_existence_check(self):
        # Must check [ -f "$path" ] to ignore rename-out events.
        self.assertIn('[ -f "$path" ]', _text())

    def test_txt_filter(self):
        # Only .txt files trigger TASK_FILE emit.
        self.assertIn("*.txt)", _text())

    def test_task_file_emit(self):
        self.assertIn("TASK_FILE:", _text())

    def test_uses_bn_basename(self):
        # Emit should use $bn (set from basename) not bare $(basename "$path").
        text = _text()
        self.assertIn('bn="$(basename "$path")"', text)
        self.assertIn("TASK_FILE: $bn", text)


class TestFswatchConfig(unittest.TestCase):
    def test_created_event(self):
        self.assertIn("--event Created", _text())

    def test_renamed_event(self):
        self.assertIn("--event Renamed", _text())

    def test_latency_half_second(self):
        self.assertIn("-l 0.5", _text())


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestScriptExists, TestCooldownPresent, TestFilters, TestFswatchConfig,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
