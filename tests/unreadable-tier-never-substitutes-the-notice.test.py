"""An unreadable tier must not journal, and must not replace her answer.

Reproduces the sonichi/sutando#4394 failure end to end: the tier could not be
read, so the result took the guarded path; journalling then failed on the SAME
condition, and `TEAM_SUPPRESS_RESULT` went to the room in place of the reply —
nine times. The journal is accountability, not confidentiality (its own
docstring), and there is no decision to account for when the tier was never read.
"""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from policy.egress.result import (  # noqa: E402
    TEAM_SUPPRESS_RESULT,
    TIER_UNREADABLE,
    guard_result_for_tier,
)

REPO = Path(__file__).resolve().parents[1]
SUPPRESSION_ONLY = "[deduped: task-abc123]"


class UnreadableTierNeverSubstitutesTheNotice(unittest.TestCase):
    def _unwritable_state_dir(self) -> str:
        """A state dir the journal cannot write — the second half of the failure."""
        d = tempfile.mkdtemp()
        os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)  # r-x: mkdir inside fails
        self.addCleanup(lambda: os.chmod(d, 0o700))
        return d

    def test_unreadable_tier_passes_the_body_through(self):
        body, _reason = guard_result_for_tier(
            SUPPRESSION_ONLY, TIER_UNREADABLE, REPO,
            suppress_journal=(self._unwritable_state_dir(), "task-abc123"))
        self.assertEqual(body, SUPPRESSION_ONLY)
        self.assertNotEqual(body, TEAM_SUPPRESS_RESULT)

    def test_guest_on_the_same_unwritable_dir_still_gets_the_notice(self):
        # Positive control: fail-closed is INTACT for a tier we actually read,
        # so the test above cannot pass by disabling the journal entirely.
        body, _reason = guard_result_for_tier(
            SUPPRESSION_ONLY, "guest", REPO,
            suppress_journal=(self._unwritable_state_dir(), "task-abc123"))
        self.assertEqual(body, TEAM_SUPPRESS_RESULT)

    def test_guest_on_a_writable_dir_journals_and_passes_through(self):
        body, _reason = guard_result_for_tier(
            SUPPRESSION_ONLY, "guest", REPO,
            suppress_journal=(tempfile.mkdtemp(), "task-abc123"))
        self.assertEqual(body, SUPPRESSION_ONLY)

    def test_owner_is_untouched(self):
        body, _reason = guard_result_for_tier(
            SUPPRESSION_ONLY, "owner", REPO,
            suppress_journal=(self._unwritable_state_dir(), "task-abc123"))
        self.assertEqual(body, SUPPRESSION_ONLY)


if __name__ == "__main__":
    unittest.main()
