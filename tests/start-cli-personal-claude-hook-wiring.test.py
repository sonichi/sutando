#!/usr/bin/env python3
"""
src/agent/claude/cli/start-cli.sh must call
scripts/install-personal-claude-hook.sh on every Claude launch, unconditionally
and before any tmux/CLAUDE_CONFIG_DIR work — the single Claude launch
chokepoint (startup.sh, --restart, menu bar, supervisor all exec this file).

Earlier revision put the call in src/agent/start-cli.sh, the generic
runtime-dispatch script shared by Claude and Codex — an adapter-edge
violation (this hook is Claude-only policy) that also fired the call for a
Codex launch. Moved here per review; this file proves the new call site and
that the generic dispatcher no longer carries Claude-specific policy.

Hermetic: real scripts/install-personal-claude-hook.sh is stubbed. The
wiring test truncates the REAL start-cli.sh source at (and including) the
install-hook call, so it proves the actual call executes, in the actual
resolver context, without needing a full tmux/claude launch (which the
sibling Chrome-seed test also deliberately avoids).
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
CALL_RE = re.compile(r'^\s*bash "\$REPO/scripts/install-personal-claude-hook\.sh"')


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
            "install-personal-claude-hook.sh call not found in "
            "src/agent/claude/cli/start-cli.sh — did it move or get removed?",
        )
        # Truncate immediately after the call so the harness never reaches the
        # tmux/CLAUDE_CONFIG_DIR machinery below it.
        truncated = "".join(lines[: call_idx + 1])
        (self.root / "src/agent/claude/cli/start-cli.sh").write_text(truncated)
        (self.root / "src/agent/claude/cli/start-cli.sh").chmod(0o755)

        shutil.copy2(
            REPO / "scripts/python-binary.sh", self.root / "scripts/python-binary.sh"
        )

        # The launcher sources this before the truncation point; without it the
        # fixture aborts under `set -euo pipefail` before the installer call.
        (self.root / "src/agent").mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            REPO / "src/agent/restart-guard.sh",
            self.root / "src/agent/restart-guard.sh",
        )

        self.marker = self.root / "installer-ran.marker"
        installer = self.root / "scripts/install-personal-claude-hook.sh"
        installer.write_text(f"#!/usr/bin/env bash\necho ran >> '{self.marker}'\n")
        installer.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def test_claude_launcher_invokes_installer_unconditionally(self):
        result = subprocess.run(
            ["/bin/bash", str(self.root / "src/agent/claude/cli/start-cli.sh")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertTrue(
            self.marker.exists(),
            "the Claude launcher did not invoke install-personal-claude-hook.sh "
            f"(stderr: {result.stderr})",
        )
        self.assertEqual(self.marker.read_text().count("ran"), 1)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_failure_does_not_abort_launch(self):
        installer = self.root / "scripts/install-personal-claude-hook.sh"
        installer.write_text("#!/usr/bin/env bash\nexit 1\n")
        installer.chmod(0o755)
        result = subprocess.run(
            ["/bin/bash", str(self.root / "src/agent/claude/cli/start-cli.sh")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # The `|| echo … >&2` fallback keeps a broken installer from taking the
        # launcher down (bash -e) while making the failure visible on stderr.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("personal-claude hook install failed", result.stderr)


class RuntimeScopingTest(unittest.TestCase):
    """The hook is Claude-only policy: it must not be wired into the generic
    dispatcher (which also routes Codex launches) or the Codex launcher."""

    def test_generic_dispatcher_does_not_call_installer(self):
        text = (REPO / "src/agent/start-cli.sh").read_text()
        self.assertNotIn(
            "install-personal-claude-hook.sh",
            text,
            "src/agent/start-cli.sh is the generic Claude/Codex dispatcher — "
            "it must not carry Claude-only hook-install policy",
        )

    def test_codex_launcher_does_not_call_installer(self):
        codex_launcher = REPO / "src/agent/codex/cli/start-cli.sh"
        self.assertTrue(codex_launcher.is_file())
        text = codex_launcher.read_text()
        self.assertNotIn(
            "install-personal-claude-hook.sh",
            text,
            "the PERSONAL_CLAUDE.md compact hook wires into Claude Code's "
            "own settings.json — Codex must never call this installer",
        )

    def test_startup_sh_does_not_call_installer(self):
        text = (REPO / "src/startup.sh").read_text()
        self.assertNotIn(
            "install-personal-claude-hook.sh",
            text,
            "startup.sh execs into src/agent/start-cli.sh for every runtime; "
            "the call belongs only at the Claude launch chokepoint",
        )


if __name__ == "__main__":
    unittest.main()
