#!/usr/bin/env python3
"""Regression test: an interrupted handoff must not destroy the previous snapshot.

The capture block streams for as long as health-check takes. Redirected straight
at session-state.md it truncates the destination at open, so any interruption
leaves a stub -- and the file is untracked with no backups, so the prior good
snapshot is gone with it. Observed 2026-08-15: a 105-byte session-state.md
containing only a timestamp and `## System Status`, the next section being the
health-check call.

Run: python3 tests/session-handoff-atomic-write.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "session-handoff.sh"


class TestAtomicHandoffWrite(unittest.TestCase):
    def setUp(self):
        self.src = SCRIPT.read_text()

    def test_capture_block_does_not_redirect_at_the_destination(self):
        """The defect in one line: `} > "$STATE_FILE"` truncates on open."""
        self.assertIsNone(
            re.search(r'^\}\s*>\s*"\$STATE_FILE"', self.src, re.M),
            "capture block redirects straight at the destination — an interrupted "
            "run truncates it and the previous snapshot is unrecoverable",
        )

    def test_capture_block_writes_to_a_stage(self):
        self.assertIsNotNone(
            re.search(r'^\}\s*>\s*"\$STATE_TMP"', self.src, re.M),
            "capture must stream into a stage, not the destination")
        self.assertIn('STATE_TMP=', self.src, "STATE_TMP must be defined")

    def test_publish_is_a_rename_gated_on_completeness(self):
        """A non-empty stage is not a complete one: the truncated file that
        motivated this was non-empty. Gate on the LAST section being present."""
        self.assertRegex(self.src, r'mv "\$STATE_TMP" "\$STATE_FILE"',
                         "publish must be a rename")
        self.assertIn("Recent Conversation", self.src.split('} > "$STATE_TMP"')[-1],
                      "the gate must key on the final section, so a partial "
                      "capture cannot publish")

    def test_incomplete_capture_leaves_the_previous_file_and_fails_loudly(self):
        tail = self.src.split('} > "$STATE_TMP"')[-1]
        self.assertIn('rm -f "$STATE_TMP"', tail, "a rejected stage must be removed")
        self.assertRegex(tail, r'exit 1',
                         "an incomplete capture must exit non-zero, not report success")

    def test_stale_stages_are_swept(self):
        """A run killed before its rename leaves a stage behind forever."""
        self.assertRegex(self.src, r'-name "\$\(basename "\$STATE_FILE"\)\.tmp\.\*"',
                         "stale stages from killed runs must be swept")

    def test_script_is_syntactically_valid(self):
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_relay_retirement_still_gated_on_the_destination(self):
        """Relay notes retire only once the capture landed. That guard reads
        $STATE_FILE, which under the fix is written only on success — so it must
        not have been repointed at the stage."""
        self.assertNotIn('[ -s "$STATE_TMP" ] && ', self.src.split("RELAY_PROCESSED")[0][-400:],
                         "relay retirement must remain gated on the published file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
