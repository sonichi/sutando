#!/usr/bin/env python3
"""PreToolUse comment-signature-guard: a published body must carry the agent's
MXID, because the shared GitHub login cannot attribute it and neither can the
commit email (hooks/comment-signature-guard.py).

Run:  python3 tests/comment-signature-guard.test.py
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "comment-signature-guard.py"
_spec = importlib.util.spec_from_file_location("csg", HOOK)
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)

MX = "qingyun-air.agent"
NEW = f"— sutando-qingyun-air (@{MX}:ag2.space)"
OLD = f"Signed: @{MX}:ag2.space (air, Qingyun's agent)"


class SignatureGuard(unittest.TestCase):
    def test_an_unsigned_inline_comment_is_flagged(self):
        self.assertIsNotNone(G.unsigned_body('gh pr comment 1 --body "findings below"'))

    def test_both_signature_formats_pass(self):
        """39 of my 41 comments use the OLDER form. A guard that accepted only
        the current wording would reject almost all of my own history."""
        self.assertIsNone(G.unsigned_body(f'gh pr comment 1 --body "x\n\n{NEW}"'))
        self.assertIsNone(G.unsigned_body(f'gh pr comment 1 --body "x\n\n{OLD}"'))

    def test_a_global_flag_before_the_subcommand_does_not_disarm_it(self):
        self.assertIsNotNone(G.unsigned_body('gh -R o/r pr comment 1 --body "x"'))
        self.assertIsNotNone(G.unsigned_body('gh --repo o/r issue comment 1 --body "x"'))

    def test_a_path_qualified_gh_is_still_gh(self):
        self.assertIsNotNone(G.unsigned_body('/opt/homebrew/bin/gh pr comment 1 --body "x"'))

    def test_a_non_publishing_subcommand_is_untouched(self):
        """`gh pr view --json body` carries no authored prose; flagging it would
        deny a large read-only class and get the hook switched off."""
        self.assertIsNone(G.unsigned_body('gh pr view 1 --json body'))
        self.assertIsNone(G.unsigned_body('gh api repos/o/r/pulls/1'))
        self.assertIsNone(G.unsigned_body('gh pr checks 1'))

    def test_body_file_is_read_from_disk(self):
        with tempfile.TemporaryDirectory() as td:
            unsigned = os.path.join(td, "a.md")
            signed = os.path.join(td, "b.md")
            open(unsigned, "w").write("findings below\n")
            open(signed, "w").write(f"findings below\n\n{NEW}\n")
            self.assertIsNotNone(G.unsigned_body(f'gh pr comment 1 --body-file {unsigned}'))
            self.assertIsNone(G.unsigned_body(f'gh pr comment 1 --body-file {signed}'))

    def test_an_unreadable_body_file_does_not_deny(self):
        """A gate that cannot read the body cannot answer, and denying on that
        would block on a path typo rather than on a missing signature."""
        self.assertIsNone(G.unsigned_body('gh pr comment 1 --body-file /nonexistent/x.md'))

    def test_the_equals_form_is_normalised(self):
        self.assertIsNotNone(G.unsigned_body('gh pr comment 1 --body="x"'))
        self.assertIsNone(G.unsigned_body(f'gh pr comment 1 --body="x {NEW}"'))

    def test_pr_and_issue_create_are_covered(self):
        self.assertIsNotNone(G.unsigned_body('gh pr create --title t --body "x"'))
        self.assertIsNotNone(G.unsigned_body('gh issue create --title t --body "x"'))


if __name__ == "__main__":
    unittest.main(verbosity=0)
