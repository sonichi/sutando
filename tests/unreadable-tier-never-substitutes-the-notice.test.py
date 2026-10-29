"""An unreadable tier must not journal, and must not replace her answer.

Reproduces the sonichi/sutando#4394 failure end to end: the tier could not be
read, so the result took the guarded path; journalling then failed on the SAME
condition, and `TEAM_SUPPRESS_RESULT` went to the room in place of the reply —
nine times. The journal is accountability, not confidentiality (its own
docstring), and there is no decision to account for when the tier was never read.
"""
import importlib
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "ag2-sparrow"))
from policy.egress.result import (  # noqa: E402
    TEAM_SUPPRESS_RESULT,
    TIER_UNREADABLE,
    guard_result_for_tier,
)

# The bridge ships a vendored copy of this policy. Testing only the src/ one
# leaves the copy that actually runs in the gateway unexercised.
_vendored = importlib.import_module("ag2_sparrow.team_result_guard")

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



class TheVendoredCopyAgrees(unittest.TestCase):
    """packages/ag2-sparrow carries its own copy of this policy and is what the
    gateway bridge imports; a fix in src/ alone leaves the running one wrong."""

    def test_the_vendored_guard_also_passes_an_unreadable_tier_through(self):
        d = tempfile.mkdtemp()
        os.chmod(d, 0o500)
        self.addCleanup(os.chmod, d, 0o700)
        body, _reason = _vendored.guard_result_for_tier(
            SUPPRESSION_ONLY, _vendored.TIER_UNREADABLE, REPO,
            suppress_journal=(d, "task-abc123"))
        self.assertEqual(body, SUPPRESSION_ONLY)
        self.assertNotEqual(body, _vendored.TEAM_SUPPRESS_RESULT,
                            "the vendored copy replaced the answer with the notice")

    def test_both_copies_exclude_the_same_sentinel(self):
        src_line = (REPO / "src" / "policy" / "egress" / "result.py").read_text()
        vend_line = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"
                     / "team_result_guard.py").read_text()
        needle = "tier != TIER_UNREADABLE and is_suppression_only(body)"
        self.assertIn(needle, src_line, "src/ lost the exclusion")
        self.assertIn(needle, vend_line, "the vendored copy lost the exclusion")

if __name__ == "__main__":
    unittest.main()
