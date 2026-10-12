#!/usr/bin/env python3
"""
The legacy hook sweep runs only where a running core can no longer depend on the
copies: in start-cli.sh past the attach/adopt exits, after the launch settings are
built, and only when those settings carry every Sutando hook. A worker launch never
sweeps, and a launch without settings (node absent, a failed build) keeps the copies.

Hermetic: the function under test is sourced from the real session-launch.sh with
src/install-claude-hooks.sh stubbed to a marker writer.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
SESSION_LAUNCH = REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh"
WORKER = REPO / "skills" / "worker-pool" / "scripts" / "launch-worker-session.sh"
CALL = "sweep_legacy_claude_hooks_for_launch"


def _line_of(lines, needle, start=0):
    return next(i for i in range(start, len(lines)) if lines[i].strip() == needle)


class LauncherOrdering(unittest.TestCase):
    def test_start_cli_sweeps_once_after_the_settings_and_every_no_op_exit(self):
        lines = LAUNCHER.read_text().splitlines()
        calls = [i for i, ln in enumerate(lines) if ln.strip() == CALL]
        self.assertEqual(len(calls), 1, "start-cli.sh must sweep exactly once")
        call = calls[0]
        self.assertLess(_line_of(lines, "resolve_claude_settings_args"), call)
        attach = _line_of(lines, "if claude_named_session_running; then")
        orphan = _line_of(lines, 'if [ -z "$RESTART_REQUESTED" ] && claude_named_process_running; then')
        self.assertLess(attach, orphan)
        self.assertLess(_line_of(lines, "fi", orphan), call, "the sweep precedes the orphan-adopt exit")
        first_launch = next(i for i, ln in enumerate(lines) if "claude --name" in ln and "#" not in ln.split("claude")[0])
        self.assertLess(call, first_launch)
        self.assertEqual(lines[call + 1].strip(), "record_core_launch_settings")

    def test_the_worker_launcher_does_not_sweep(self):
        text = WORKER.read_text()
        for needle in (CALL, "sweep_project_claude_hooks", "install-claude-hooks.sh"):
            self.assertNotIn(needle, text)

    def test_generic_dispatcher_codex_and_startup_do_not_sweep(self):
        for rel in ("src/agent/start-cli.sh", "src/agent/codex/cli/start-cli.sh", "src/startup.sh"):
            text = (REPO / rel).read_text()
            for needle in (CALL, "install-claude-hooks.sh"):
                self.assertNotIn(needle, text, f"{rel} must not carry the Claude-only sweep")


@unittest.skipUnless(shutil.which("node"), "node is required to build the launch settings")
class GatedSweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        for rel in ("src/agent/claude/cli/session-launch.sh", "src/agent/claude/cli/build-core-settings.mjs",
                    "src/agent/claude/cli/owned-hooks.json", "src/claude_hooks_settings.py",
                    "src/skill_hooks.py"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, self.root / rel)
        self.marker = self.root / "sweep-ran.marker"
        installer = self.root / "src/install-claude-hooks.sh"
        installer.write_text(f"#!/usr/bin/env bash\necho ran >> '{self.marker}'\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _built(self):
        return subprocess.run(
            ["node", str(self.root / "src/agent/claude/cli/build-core-settings.mjs"), "/g.py",
             "--owned-hooks", str(self.root)], capture_output=True, text=True, check=True).stdout

    def _sweep(self, settings_args):
        script = ('. "$REPO/src/agent/claude/cli/session-launch.sh"; PY=python3; SETTINGS_ARGS=("$@"); '
                  f'{CALL}; echo "rc=$?"')
        return subprocess.run(["/bin/bash", "-c", script, "x", *settings_args], capture_output=True, text=True,
                              timeout=30, env={**os.environ, "REPO": str(self.root)})

    def test_carried_settings_sweep_once(self):
        r = self._sweep(["--settings", self._built()])
        self.assertIn("rc=0", r.stdout, r.stderr)
        self.assertEqual(self.marker.read_text().count("ran"), 1)

    def test_no_settings_keeps_the_copies(self):
        r = self._sweep([])
        self.assertIn("rc=0", r.stdout)
        self.assertFalse(self.marker.exists())
        self.assertIn("legacy hook entries kept", r.stderr)

    def test_settings_missing_a_hook_keep_the_copies(self):
        partial = json.loads(self._built())
        partial["hooks"].pop("UserPromptSubmit")
        r = self._sweep(["--settings", json.dumps(partial)])
        self.assertIn("rc=0", r.stdout)
        self.assertFalse(self.marker.exists())
        self.assertIn("lack a Sutando hook", r.stderr)

    def test_a_failing_sweep_does_not_abort_the_launch(self):
        (self.root / "src/install-claude-hooks.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
        r = self._sweep(["--settings", self._built()])
        self.assertIn("rc=0", r.stdout)
        self.assertIn("legacy hook sweep failed", r.stderr)


if __name__ == "__main__":
    unittest.main()
