#!/usr/bin/env python3
"""Blocker #1 (sonichi#4303 review): agy is not wired into core selection
(src/agent/agy/README.md), so its task-notifier.sh watcher runs ALONGSIDE
the existing Claude/Codex core's canonical watcher, not instead of it.
Defaulting to the shared `workspace/tasks` + `results` dirs meant BOTH
watchers processed every task — including an irreversible one — twice.

This pins the fix: with no explicit SUTANDO_TASKS_DIR/SUTANDO_RESULTS_DIR,
agy's notifier resolves to a separately-owned `tasks-agy`/`results-agy`
inbox, never the canonical one. An explicit override (every other test in
this suite uses one) is unaffected.

Extracts the real resolution block verbatim from task-notifier.sh (rather
than re-typing it) so this test exercises the actual shipped logic.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NOTIFIER = REPO / "src/agent/agy/cli/task-notifier.sh"


def _resolution_block() -> str:
    src = NOTIFIER.read_text()
    start = src.index('if [ -n "${SUTANDO_TASKS_DIR:-}" ]')
    end = src.index("\nPOLL_INTERVAL=")
    return src[start:end]


class DirResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake_repo = Path(self.tmp.name)
        (self.fake_repo / "scripts").mkdir(parents=True)
        self.fake_workspace = self.fake_repo / "workspace"
        config_script = self.fake_repo / "scripts" / "sutando-config.sh"
        config_script.write_text(f'#!/bin/bash\necho "{self.fake_workspace}"\n')
        config_script.chmod(0o755)
        self.block = _resolution_block()
        self.assertIn("TASKS_DIR=", self.block)
        self.assertIn("RESULTS_DIR=", self.block)

    def _resolve(self, env_lines):
        script = (
            "set -euo pipefail\n"
            f'REPO="{self.fake_repo}"\n'
            "unset SUTANDO_TASKS_DIR SUTANDO_RESULTS_DIR 2>/dev/null || true\n"
            + "\n".join(env_lines) + "\n"
            + self.block + "\n"
            'echo "TASKS_DIR=$TASKS_DIR"\n'
            'echo "RESULTS_DIR=$RESULTS_DIR"\n'
        )
        result = subprocess.run(["/bin/bash", "-c", script],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.strip().splitlines())

    def test_default_dirs_are_a_separate_agy_owned_inbox(self):
        out = self._resolve([])
        self.assertEqual(out["TASKS_DIR"], str(self.fake_workspace / "tasks-agy"))
        self.assertEqual(out["RESULTS_DIR"], str(self.fake_workspace / "results-agy"))
        # The regression this pins: must NOT be the canonical shared dirs
        # the existing Claude/Codex core watcher also watches.
        self.assertNotEqual(out["TASKS_DIR"], str(self.fake_workspace / "tasks"))
        self.assertNotEqual(out["RESULTS_DIR"], str(self.fake_workspace / "results"))

    def test_explicit_tasks_dir_override_still_works(self):
        custom = self.fake_repo / "custom-tasks"
        out = self._resolve([f'export SUTANDO_TASKS_DIR="{custom}"'])
        self.assertEqual(out["TASKS_DIR"], str(custom))
        # No explicit RESULTS_DIR override: derives the sibling results/ dir,
        # same convention as before this fix (and as every other consumer).
        self.assertEqual(out["RESULTS_DIR"], str(custom.parent / "results"))

    def test_explicit_both_overrides_are_honored_verbatim(self):
        custom_tasks = self.fake_repo / "custom-tasks"
        custom_results = self.fake_repo / "custom-results"
        out = self._resolve([
            f'export SUTANDO_TASKS_DIR="{custom_tasks}"',
            f'export SUTANDO_RESULTS_DIR="{custom_results}"',
        ])
        self.assertEqual(out["TASKS_DIR"], str(custom_tasks))
        self.assertEqual(out["RESULTS_DIR"], str(custom_results))


if __name__ == "__main__":
    unittest.main()
