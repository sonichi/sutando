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

    def test_the_detached_publish_sits_behind_the_liveness_gate(self):
        """Presence is not the question — ordering is. A publish before the gate
        can replace a truthful marker with a runtime that never came up."""
        src = CLAUDE.read_text(encoding="utf-8")
        gate = src.index("did not come up within")
        pub = src.index("publish_active_runtime", src.index("new-session -d"))
        self.assertGreater(pub, gate,
                           "the detached path must publish only after the liveness check")

    def test_the_exec_path_publishes_immediately_before_exec(self):
        """exec leaves no post-launch point; the call must at least be adjacent."""
        src = CLAUDE.read_text(encoding="utf-8")
        i = src.index('exec tmux -S "$TMUX_SOCKET" new-session')
        window = src[max(0, i - 220):i]
        self.assertIn("publish_active_runtime", window)

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


    def test_codex_publishes_only_after_the_session_is_created(self):
        """Ordering, not presence. Publishing before creation replaces a
        truthful marker with a runtime that never came up."""
        src = CODEX.read_text(encoding="utf-8")
        launch = src.index("new-session -d")
        self.assertGreater(src.index("publish_active_runtime", launch), launch,
                           "codex must publish only after tmux new-session")

    def _run_codex_publish(self, tmp, tmux_rc):
        """The real publish function plus its real guarded call site, with tmux
        forced to `tmux_rc`. Slicing further would cut an unbalanced if-block."""
        src = CODEX.read_text(encoding="utf-8")
        i = src.index("publish_active_runtime() {")
        fn = src[i:src.index("\n}\n", i) + 3]
        gated = [l.strip() for l in src.splitlines()
                 if "has-session" in l and "publish_active_runtime" in l]
        self.assertTrue(gated, "no gated publish call in the codex launcher — the "
                               "publish is ungated, which is the defect this pins")
        call = gated[0]
        harness = (
            "set -uo pipefail\n"
            f'REPO="{REPO}"\nSESSION="sutando-core"\nTMUX_SOCKET="/tmp/none"\n'
            f'sutando_config() {{ printf "%s" "{Path(tmp) / "workspace"}"; }}\n'
            f"tmux() {{ return {tmux_rc}; }}\n"
            + fn.replace('bash "$REPO/scripts/sutando-config.sh" workspace', "sutando_config")
            + "\n" + call + "\n"
        )
        subprocess.run(["/bin/bash", "-c", harness], capture_output=True, text=True, cwd=tmp)

    def test_a_failed_codex_launch_leaves_the_previous_marker_truthful(self):
        """Claude->Codex where tmux refuses: the marker must still say claude."""
        before = {"runtime": "claude", "session": "sutando-core", "started_at": 1}
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace" / "state"
            ws.mkdir(parents=True)
            (ws / "core-runtime.json").write_text(json.dumps(before), encoding="utf-8")
            self._run_codex_publish(tmp, 42)
            after = json.loads((ws / "core-runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(after, before,
                             "a codex launch that never came up must not rewrite the marker")

        # Positive control: a snippet that does nothing would also leave the
        # marker unchanged, so prove the same harness DOES publish on success.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspace" / "state"
            ws.mkdir(parents=True)
            (ws / "core-runtime.json").write_text(json.dumps(before), encoding="utf-8")
            self._run_codex_publish(tmp, 0)
            after = json.loads((ws / "core-runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(after.get("runtime"), "codex",
                             "control failed: the harness never publishes, so the "
                             "failure case above proves nothing")


if __name__ == "__main__":
    unittest.main(verbosity=0)
