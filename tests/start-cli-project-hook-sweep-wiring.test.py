#!/usr/bin/env python3
"""
Every Claude launch (core: src/agent/claude/cli/start-cli.sh; pool worker:
launch-worker-session.sh) must run sweep_project_claude_hooks, which removes the
hook entries older installers left in project settings files. Without it those
copies keep firing in every session opened in the checkout, and twice in the core.

Hermetic: src/install-claude-hooks.sh is stubbed. The REAL start-cli.sh source is
truncated right after the call, so the actual call runs in its real context
without a tmux/claude launch.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
SESSION_LAUNCH = REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh"
CALL_RE = re.compile(r'^\s*sweep_project_claude_hooks\s*$')


class StartCliPersonalClaudeHookWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "src/agent/claude/cli").mkdir(parents=True)
        (self.root / "scripts").mkdir(parents=True)

        lines = LAUNCHER.read_text().splitlines(keepends=True)
        call_idx = next((i for i, ln in enumerate(lines) if CALL_RE.match(ln)), None)
        self.assertIsNotNone(
            call_idx,
            "sweep_project_claude_hooks call not found in "
            "src/agent/claude/cli/start-cli.sh — did it move or get removed?",
        )
        self.assertIn(
            'bash "$REPO/src/install-claude-hooks.sh"',
            SESSION_LAUNCH.read_text(),
            "sweep_project_claude_hooks in session-launch.sh no longer "
            "calls src/install-claude-hooks.sh",
        )
        # Truncate immediately after the call so the harness never reaches the
        # tmux/CLAUDE_CONFIG_DIR machinery below it.
        truncated = "".join(lines[: call_idx + 1])
        (self.root / "src/agent/claude/cli/start-cli.sh").write_text(truncated)
        (self.root / "src/agent/claude/cli/start-cli.sh").chmod(0o755)
        shutil.copy2(
            SESSION_LAUNCH, self.root / "src/agent/claude/cli/session-launch.sh"
        )

        shutil.copy2(
            REPO / "scripts/python-binary.sh", self.root / "scripts/python-binary.sh"
        )

        # The launcher sources this before the truncation point; without it the
        # fixture aborts under `set -euo pipefail` before the sweep call.
        (self.root / "src/agent").mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            REPO / "src/agent/restart-guard.sh",
            self.root / "src/agent/restart-guard.sh",
        )
        shutil.copy2(
            REPO / "src/agent/task-event-handler-lookup.sh",
            self.root / "src/agent/task-event-handler-lookup.sh",
        )

        self.marker = self.root / "sweep-ran.marker"
        installer = self.root / "src/install-claude-hooks.sh"
        installer.write_text(f"#!/usr/bin/env bash\necho ran >> '{self.marker}'\n")
        installer.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args):
        return subprocess.run(
            ["/bin/bash", str(self.root / "src/agent/claude/cli/start-cli.sh"), *args],
            capture_output=True, text=True, timeout=30,
        )

    def test_claude_launcher_runs_the_sweep_unconditionally(self):
        result = self._run()
        self.assertTrue(self.marker.exists(),
                        f"the Claude launcher did not run the sweep (stderr: {result.stderr})")
        self.assertEqual(self.marker.read_text().count("ran"), 1)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_print_core_env_probe_does_not_sweep(self):
        result = self._run("--print-core-env")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists(), "a pure-read probe must not write settings")

    def test_sweep_failure_does_not_abort_launch(self):
        installer = self.root / "src/install-claude-hooks.sh"
        installer.write_text("#!/usr/bin/env bash\nexit 1\n")
        installer.chmod(0o755)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("project hook sweep failed", result.stderr)


class RuntimeScopingTest(unittest.TestCase):
    """Claude-only policy: not in the generic dispatcher, Codex, or startup.sh."""

    def test_generic_dispatcher_codex_and_startup_do_not_sweep(self):
        for rel in ("src/agent/start-cli.sh", "src/agent/codex/cli/start-cli.sh", "src/startup.sh"):
            text = (REPO / rel).read_text()
            for needle in ("sweep_project_claude_hooks", "install-claude-hooks.sh"):
                self.assertNotIn(needle, text, f"{rel} must not carry the Claude-only sweep")

    def test_the_worker_launcher_also_calls_it(self):
        worker_launcher = REPO / "skills/worker-pool/scripts/launch-worker-session.sh"
        self.assertRegex(worker_launcher.read_text(), r"(?m)^sweep_project_claude_hooks$")


if __name__ == "__main__":
    unittest.main()
