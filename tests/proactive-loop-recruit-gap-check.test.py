#!/usr/bin/env python3
"""Contract for the recruit gap check: nothing blocks and nobody has been asked."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "skills" / "proactive-loop" / "scripts" / "recruit-gap-check.py"
spec = importlib.util.spec_from_file_location("recruit_gap_check", MOD)
g = importlib.util.module_from_spec(spec)
sys.modules["recruit_gap_check"] = g
spec.loader.exec_module(g)

SHARED = ["shared-login", "other-shared"]


def rv(login, state, ts="2026-09-01T00:00:00Z"):
    return {"submitted_at": ts, "state": state, "user": {"login": login}}


class LatestStates(unittest.TestCase):
    def test_newest_state_per_reviewer_wins(self):
        got = g.latest_states([rv("a", "CHANGES_REQUESTED"), rv("a", "APPROVED")])
        self.assertEqual(got, {"a": "APPROVED"})

    def test_commented_is_not_a_verdict(self):
        """A remark must not stand in for a state: reviewDecision ignores COMMENTED."""
        got = g.latest_states([rv("a", "APPROVED"), rv("a", "COMMENTED")])
        self.assertEqual(got, {"a": "APPROVED"})

    def test_a_reviewer_with_only_comments_is_absent(self):
        self.assertEqual(g.latest_states([rv("a", "COMMENTED")]), {})

    def test_missing_user_is_skipped_not_crashed(self):
        self.assertEqual(g.latest_states([{"state": "APPROVED", "user": {}}]), {})


class Classify(unittest.TestCase):
    def test_a_standing_block_outranks_a_short_count(self):
        v = g.classify(2, {"a": "CHANGES_REQUESTED"}, SHARED)
        self.assertEqual(v["verdict"], g.BLOCKED)
        self.assertEqual(v["blocking"], ["a"])

    def test_nothing_blocking_and_short_is_the_recruit_case(self):
        v = g.classify(2, {"a": "APPROVED"}, SHARED)
        self.assertEqual(v["verdict"], g.RECRUIT)
        self.assertEqual(v["github_short_by"], 1)

    def test_zero_approvals_and_no_block_still_recruits(self):
        v = g.classify(2, {}, SHARED)
        self.assertEqual(v["verdict"], g.RECRUIT)
        self.assertEqual(v["github_short_by"], 2)

    def test_the_shared_login_COUNTS_for_github_but_identifies_nobody(self):
        """The bar is met, so it is not a blocker -- but only one party vouched."""
        v = g.classify(2, {"a": "APPROVED", SHARED[0]: "APPROVED"}, SHARED)
        self.assertEqual(v["verdict"], g.THIN)
        self.assertEqual(v["github_short_by"], 0, "GitHub counts the shared login")
        self.assertEqual(v["distinct_short_by"], 1, "but one of the two is not a distinct party")

    def test_a_SECOND_shared_login_is_also_not_a_distinct_party(self):
        """#3482: more than one account is shared, so a single-login check miscounts."""
        v = g.classify(2, {"shared-login": "APPROVED", "other-shared": "APPROVED"}, SHARED)
        self.assertEqual(v["github_short_by"], 0, "GitHub counts both")
        self.assertEqual(v["distinct_short_by"], 2, "neither identifies a party")
        self.assertEqual(v["verdict"], g.THIN)

    def test_a_plain_string_is_accepted_as_one_shared_login(self):
        v = g.classify(2, {"a": "APPROVED", "s": "APPROVED"}, "s")
        self.assertEqual(v["distinct"], ["a"])

    def test_two_distinct_approvals_meet_the_bar(self):
        v = g.classify(2, {"a": "APPROVED", "b": "APPROVED"}, SHARED)
        self.assertEqual(v["verdict"], g.MET)
        self.assertEqual((v["github_short_by"], v["distinct_short_by"]), (0, 0))

    def test_a_block_beside_enough_approvals_is_still_blocked(self):
        v = g.classify(2, {"a": "APPROVED", "b": "APPROVED", "c": "CHANGES_REQUESTED"}, SHARED)
        self.assertEqual(v["verdict"], g.BLOCKED)

    def test_a_zero_bar_never_recruits(self):
        self.assertEqual(g.classify(0, {}, SHARED)["verdict"], g.MET)


class Render(unittest.TestCase):
    def test_every_verdict_names_the_pr_and_the_counts(self):
        for latest in ({"a": "CHANGES_REQUESTED"}, {"a": "APPROVED"},
                       {"a": "APPROVED", SHARED[0]: "APPROVED"},
                       {"a": "APPROVED", "b": "APPROVED"}):
            line = g.render(99, g.classify(2, latest, SHARED), SHARED)
            self.assertIn("#99", line)
            self.assertIn("/2", line, f"the bar must be visible in: {line}")

    def test_the_recruit_line_says_who_approved(self):
        line = g.render(7, g.classify(2, {"a": "APPROVED"}, SHARED), SHARED)
        self.assertIn("RECRUIT", line)
        self.assertIn("a", line)

    def test_the_recruit_line_names_nobody_when_nobody_approved(self):
        line = g.render(7, g.classify(2, {}, SHARED), SHARED)
        self.assertIn("nobody", line)


if __name__ == "__main__":
    unittest.main(verbosity=1)
