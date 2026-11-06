"""An unreadable task file is not a tier.

`resolve_access_tier` used to answer `guest` for BOTH a malformed explicit tier
and a file it could not read. On a host with intermittent EPERM that made an
owner task read as guest, which routed it down the guarded path and delivered a
notice in place of her answer (sonichi/sutando#4394). The two inputs are
different and must answer differently; privilege still fails closed either way.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from policy.egress.result import (  # noqa: E402
    TIER_UNREADABLE,
    is_guarded_tier,
    resolve_access_tier,
)


class AccessTierUnreadable(unittest.TestCase):
    def _task(self, body: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_unreadable_answers_the_sentinel_not_guest(self):
        # The defect: an I/O failure reported as a tier the file does not carry.
        self.assertEqual(resolve_access_tier("/nonexistent/dir/task-x.txt"), TIER_UNREADABLE)

    def test_malformed_explicit_tier_still_answers_guest(self):
        # Positive control: the fix must not move the malformed case, which is
        # a real decision about a file we DID read.
        self.assertEqual(resolve_access_tier(self._task("access_tier: bogus\ntask: hi")), "guest")

    def test_conflicting_tiers_still_answer_guest(self):
        two = "access_tier: owner\naccess_tier: guest\ntask: hi"
        self.assertEqual(resolve_access_tier(self._task(two)), "guest")

    def test_owner_and_team_are_unaffected(self):
        self.assertEqual(resolve_access_tier(self._task("access_tier: owner\ntask: hi")), "owner")
        self.assertEqual(resolve_access_tier(self._task("access_tier: team\ntask: hi")), "team")

    def test_the_sentinel_cannot_be_forged_by_a_task_file(self):
        # `unreadable` is outside the legal set, so a file claiming it reads as
        # guest — a sentinel a caller could spoof would not be one.
        forged = self._task(f"access_tier: {TIER_UNREADABLE}\ntask: hi")
        self.assertEqual(resolve_access_tier(forged), "guest")

    def test_privilege_still_fails_closed_for_the_sentinel(self):
        # The point of the change is disposition, never privilege: an
        # unreadable tier must stay guarded, exactly as guest is.
        self.assertTrue(is_guarded_tier(TIER_UNREADABLE))
        self.assertTrue(is_guarded_tier("guest"))
        self.assertFalse(is_guarded_tier("owner"))


if __name__ == "__main__":
    unittest.main()
