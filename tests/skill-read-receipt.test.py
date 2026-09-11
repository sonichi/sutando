#!/usr/bin/env python3
"""Controls for scripts/skill-read-receipt.py — every exit path, both directions.

A checker never observed FAILING is not a validated checker, so each assertion here
drives the script into the state it is meant to catch and asserts the non-zero, not
only the clean path.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "skill-read-receipt.py"


def run(args, session="sess-A"):
    env = dict(os.environ)
    if session is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env["CLAUDE_CODE_SESSION_ID"] = ""
    else:
        env["CLAUDE_CODE_SESSION_ID"] = session
    p = subprocess.run([sys.executable, str(SCRIPT)] + args,
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


class ReceiptContract(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.skill = self.d / "SKILL.md"
        self.skill.write_text("alpha\nbeta\n")
        self.state = self.d / "state"

    def _base(self):
        return ["--skill", str(self.skill), "--state-dir", str(self.state)]

    def test_no_receipt_demands_a_full_read(self):
        rc, out = run(self._base() + ["--check"])
        self.assertEqual(rc, 1, out)
        self.assertIn("FULL READ REQUIRED", out)

    def test_record_then_check_passes(self):
        self.assertEqual(run(self._base() + ["--record"])[0], 0)
        rc, out = run(self._base() + ["--check"])
        self.assertEqual(rc, 0, out)

    def test_changed_bytes_invalidate_the_receipt(self):
        run(self._base() + ["--record"])
        self.skill.write_text("alpha\nbeta CHANGED\n")
        rc, out = run(self._base() + ["--check"])
        self.assertEqual(rc, 1, out)
        self.assertIn("content changed", out)

    def test_another_sessions_receipt_is_never_reused(self):
        run(self._base() + ["--record"], session="sess-A")
        rc, out = run(self._base() + ["--check"], session="sess-B")
        self.assertEqual(rc, 1, out)

    def test_unscoped_record_is_refused(self):
        rc, out = run(self._base() + ["--record"], session=None)
        self.assertEqual(rc, 2, out)
        self.assertIn("REFUSED", out)

    def test_missing_file_cannot_answer(self):
        rc, out = run(["--skill", str(self.d / "nope.md"),
                       "--state-dir", str(self.state), "--check"])
        self.assertEqual(rc, 2, out)

    def test_absent_marker_cannot_answer(self):
        rc, out = run(self._base() + ["--marker", "NOT PRESENT", "--check"])
        self.assertEqual(rc, 2, out)

    def test_marker_defaults_to_none_so_the_tool_is_generic(self):
        """The pr-triage original defaulted to its own HARNESS-GREEN clause; a
        generic tool that kept that default would refuse every other skill."""
        self.assertEqual(run(self._base() + ["--record"])[0], 0)

    # --- audit -------------------------------------------------------------
    def test_audit_clean_when_nothing_changed(self):
        run(self._base() + ["--record"])
        rc, out = run(["--state-dir", str(self.state), "--audit"])
        self.assertEqual(rc, 0, out)
        self.assertIn("0 drifted", out)

    def test_audit_FAILS_when_a_read_file_changed_underneath(self):
        run(self._base() + ["--record"])
        self.skill.write_text("alpha\nbeta CHANGED\n")
        rc, out = run(["--state-dir", str(self.state), "--audit"])
        self.assertEqual(rc, 1, out)
        self.assertIn("DRIFTED", out)

    def test_audit_FAILS_when_a_read_file_was_deleted(self):
        run(self._base() + ["--record"])
        self.skill.unlink()
        rc, out = run(["--state-dir", str(self.state), "--audit"])
        self.assertEqual(rc, 1, out)
        self.assertIn("GONE", out)

    def test_audit_ignores_other_sessions(self):
        run(self._base() + ["--record"], session="sess-A")
        self.skill.write_text("changed\n")
        rc, out = run(["--state-dir", str(self.state), "--audit"], session="sess-B")
        self.assertEqual(rc, 0, out)
        self.assertIn("no receipts for this session", out)

    def test_audit_without_a_session_cannot_answer(self):
        rc, out = run(["--state-dir", str(self.state), "--audit"], session=None)
        self.assertEqual(rc, 2, out)

    def test_no_receipts_is_clean_not_a_failure(self):
        rc, out = run(["--state-dir", str(self.d / "empty"), "--audit"])
        self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
