#!/usr/bin/env python3
"""The active-runtime marker is owned by whichever launcher actually comes up.

A refused or failed restart must leave the previous marker truthful, so the
switch path may write desired state only.
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SWITCH = REPO / "src" / "agent" / "start-cli.sh"
CLAUDE = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
CODEX = REPO / "src" / "agent" / "codex" / "cli" / "start-cli.sh"


class MarkerOwnership(unittest.TestCase):
    def test_switch_path_never_writes_the_active_marker(self):
        code = re.sub(r"(?m)^\s*#.*$", "", SWITCH.read_text(encoding="utf-8"))
        self.assertNotIn("core-runtime.json", code,
                         "the switch path must write desired state only")

    def test_both_launchers_publish_the_marker(self):
        for f in (CLAUDE, CODEX):
            self.assertIn("core-runtime.json", f.read_text(encoding="utf-8"),
                          f"{f.name} must publish the active runtime when it comes up")

    def test_a_refused_switch_leaves_the_previous_marker_truthful(self):
        """Codex->Claude: the switch runs, the restart never does."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace" / "state"
            ws.mkdir(parents=True)
            before = {"runtime": "codex", "session": "sutando-core", "started_at": 1}
            (ws / "core-runtime.json").write_text(json.dumps(before), encoding="utf-8")

            # everything the switch path does, minus the launcher it never reaches
            src = SWITCH.read_text(encoding="utf-8")
            i = src.index('if [ -n "$requested_runtime" ]')
            j = src.index("\nfi\n", i)
            snippet = src[i:j]
            harness = (
                "set -euo pipefail\n"
                f'REPO="{REPO}"\n'
                'requested_runtime="claude"\n'
                f'sutando_config() {{ printf "%s" "{Path(tmp) / "workspace"}"; }}\n'
                + snippet.replace(
                    'bash "$REPO/scripts/sutando-config.sh" workspace', 'sutando_config')
                + "\nfi\n"
            )
            subprocess.run(["/bin/bash", "-c", harness], capture_output=True, text=True,
                           cwd=tmp)

            after = json.loads((ws / "core-runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(after, before,
                             "a switch that never launched must not rewrite the marker")


if __name__ == "__main__":
    unittest.main(verbosity=0)
