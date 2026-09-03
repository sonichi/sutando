#!/usr/bin/env python3
"""An interrupted handoff must not leak its stage, and a failed publish must keep it.

Source-tied: the trap is extracted and executed, because running the real script
resolves the live workspace and would overwrite session-state.md.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "session-handoff.sh"
BASH = shutil.which("bash") or "/bin/bash"


def _trap_snippet() -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"(_handoff_keep_stage=0\n.*?kill -TERM \$\$' TERM)", src, re.DOTALL)
    assert m, "stage-cleanup trap not found in session-handoff.sh"
    return m.group(1)


class StageCleanup(unittest.TestCase):
    def test_a_cancelled_run_dies_and_leaves_no_stage(self):
        """Cleanup is not enough: a trap replaces the default action, so a handler
        that returns lets the cancelled hook run on through the rest of capture."""
        with tempfile.TemporaryDirectory() as tmp:
            after = Path(tmp) / "downstream-ran"
            harness = (
                f'STATE_FILE="{tmp}/session-state.md"\n'
                'STATE_TMP="$(mktemp "${STATE_FILE}.tmp.XXXXXX")"\n'
                'echo staged > "$STATE_TMP"\n'
                + _trap_snippet() + "\n"
                'kill -TERM $$\n'
                f'touch "{after}"\n'          # must never run
                'sleep 5\n'
            )
            r = subprocess.run([BASH, "-c", harness], capture_output=True, timeout=30)
            self.assertEqual(r.returncode, -15,
                             f"cancelled hook must die on SIGTERM, got rc={r.returncode}")
            self.assertFalse(after.exists(),
                             "work downstream of the signal ran — the trap swallowed it")
            self.assertEqual(list(Path(tmp).glob("session-state.md.tmp.*")), [],
                             "cancelled run leaked its stage")

    def test_the_publish_failure_path_keeps_the_stage(self):
        """The stage is the only copy of a completed capture — a blanket rm loses it."""
        with tempfile.TemporaryDirectory() as tmp:
            harness = (
                f'STATE_FILE="{tmp}/session-state.md"\n'
                'STATE_TMP="$(mktemp "${STATE_FILE}.tmp.XXXXXX")"\n'
                'echo staged > "$STATE_TMP"\n'
                + _trap_snippet() + "\n"
                '_handoff_keep_stage=1\n'      # what the publish-failure branch sets
                'exit 1\n'
            )
            subprocess.run([BASH, "-c", harness], capture_output=True, timeout=30)
            kept = list(Path(tmp).glob("session-state.md.tmp.*"))
            self.assertEqual(len(kept), 1,
                             "a failed publish must keep the only copy of the capture")

    def test_the_publish_failure_branch_still_opts_out(self):
        src = SCRIPT.read_text(encoding="utf-8")
        i = src.index("publish failed")
        window = src[max(0, i - 400):i]
        self.assertIn("_handoff_keep_stage=1", window,
                      "the publish-failure branch must opt out of stage cleanup")


if __name__ == "__main__":
    unittest.main(verbosity=0)
