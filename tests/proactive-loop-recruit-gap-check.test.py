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

    # --- ineligible approvals (keweichen, #4055) ---------------------------
    def test_an_ineligible_approval_counts_toward_NEITHER_total(self):
        """#3698 shape: TustinOC (not a collaborator) + john-the-dev + a
        shared login all approved -- eligible+distinct is really just
        john-the-dev, one short of 2."""
        v = g.classify(2, {"TustinOC": "APPROVED", "john-the-dev": "APPROVED",
                            SHARED[0]: "APPROVED"}, SHARED,
                        ineligible={"TustinOC"})
        self.assertEqual(v["counted"], ["john-the-dev", SHARED[0]])
        self.assertEqual(v["verdict"], g.THIN)
        self.assertEqual(v["distinct_short_by"], 1)

    def test_CONTROL_without_the_ineligible_set_the_bug_reproduces(self):
        """Same inputs, default `ineligible` -- the pre-fix shape: TustinOC's
        approval satisfies the bar outright, MET instead of THIN."""
        v = g.classify(2, {"TustinOC": "APPROVED", "john-the-dev": "APPROVED",
                            SHARED[0]: "APPROVED"}, SHARED)
        self.assertEqual(v["verdict"], g.MET)

    def test_an_ineligible_only_approval_still_recruits(self):
        v = g.classify(1, {"TustinOC": "APPROVED"}, SHARED, ineligible={"TustinOC"})
        self.assertEqual(v["verdict"], g.RECRUIT)


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


class Gh(unittest.TestCase):
    """The subprocess boundary: a non-zero gh must raise, never return a partial answer."""

    def _run(self, rc, out="", err=""):
        class R:
            returncode, stdout, stderr = rc, out, err
        return lambda *a, **k: R()

    def test_a_clean_call_returns_parsed_json(self):
        orig = g.subprocess.run
        g.subprocess.run = self._run(0, '[{"type": "pull_request"}]')
        try:
            self.assertEqual(g._gh(["x"]), [{"type": "pull_request"}])
        finally:
            g.subprocess.run = orig

    def test_a_failing_call_raises_rather_than_returning_empty(self):
        orig = g.subprocess.run
        g.subprocess.run = self._run(1, "", "Not Found")
        try:
            with self.assertRaises(RuntimeError) as cm:
                g._gh(["x"])
            self.assertIn("Not Found", str(cm.exception))
        finally:
            g.subprocess.run = orig

    def test_is_collaborator_204_is_true(self):
        orig = g.subprocess.run
        g.subprocess.run = self._run(0)
        try:
            self.assertIs(g.is_collaborator("o/r", "a"), True)
        finally:
            g.subprocess.run = orig

    def test_is_collaborator_404_is_false(self):
        orig = g.subprocess.run
        g.subprocess.run = self._run(1, "", "gh: Not Found (HTTP 404)")
        try:
            self.assertIs(g.is_collaborator("o/r", "a"), False)
        finally:
            g.subprocess.run = orig

    def test_is_collaborator_ANYTHING_ELSE_is_undetermined_not_false(self):
        """A rate limit, a network blip, an auth failure -- none of these is
        evidence the login lacks access, and guessing False would let a real
        collaborator's approval be silently dropped from the count."""
        orig = g.subprocess.run
        g.subprocess.run = self._run(1, "", "gh: API rate limit exceeded (HTTP 403)")
        try:
            self.assertIsNone(g.is_collaborator("o/r", "a"))
        finally:
            g.subprocess.run = orig


class RequiredApprovals(unittest.TestCase):
    def _with(self, payload):
        orig = g._gh
        g._gh = lambda args: payload
        self.addCleanup(lambda: setattr(g, "_gh", orig))

    def test_it_reads_the_count_off_the_pull_request_rule(self):
        self._with([{"type": "deletion", "parameters": {}},
                    {"type": "pull_request",
                     "parameters": {"required_approving_review_count": 2}}])
        self.assertEqual(g.required_approvals("o/r", "main"), 2)

    def test_no_pull_request_rule_means_UNKNOWN_not_zero(self):
        """A missing rule states nothing; 0 is a SATISFIED bar (yixuan-ag2,
        #4055): the old `return 0` read "no rule" as "bar met" for a PR
        with zero approvals on any branch lacking a ruleset."""
        self._with([{"type": "deletion", "parameters": {}}])
        self.assertIsNone(g.required_approvals("o/r", "main"))


class Main(unittest.TestCase):
    def _stub(self, rules, reviews_by_pr):
        orig, orig_collab = g._gh, g.is_collaborator
        self.gh_calls = []

        def fake(args):
            self.gh_calls.append(args[0])
            if "rules/branches" in args[0]:
                return rules
            pr = args[0].split("/pulls/")[1].split("/")[0]
            got = reviews_by_pr.get(pr)
            if isinstance(got, Exception):
                raise got
            return got
        g._gh = fake
        g.is_collaborator = lambda repo, login: True  # every approver eligible unless overridden
        self.addCleanup(lambda: setattr(g, "_gh", orig))
        self.addCleanup(lambda: setattr(g, "is_collaborator", orig_collab))

    RULES = [{"type": "pull_request", "parameters": {"required_approving_review_count": 2}}]

    def test_recruit_exits_1(self):
        self._stub(self.RULES, {"7": [rv("a", "APPROVED")]})
        self.assertEqual(g.main(["--repo", "o/r", "--shared-login", SHARED[0], "7"]), 1)

    def test_a_met_bar_exits_0(self):
        self._stub(self.RULES, {"7": [rv("a", "APPROVED"), rv("b", "APPROVED")]})
        self.assertEqual(g.main(["--repo", "o/r", "--shared-login", SHARED[0], "7"]), 0)

    def test_a_blocked_pr_exits_0_because_recruiting_is_premature(self):
        self._stub(self.RULES, {"7": [rv("a", "CHANGES_REQUESTED")]})
        self.assertEqual(g.main(["--repo", "o/r", "--shared-login", SHARED[0], "7"]), 0)

    def test_an_unreadable_pr_exits_2_and_does_not_abort_the_rest(self):
        self._stub(self.RULES, {"7": RuntimeError("boom"), "8": [rv("a", "APPROVED")]})
        self.assertEqual(g.main(["--repo", "o/r", "--shared-login", SHARED[0], "7", "8"]), 2)

    def test_an_unreadable_RULESET_exits_2_without_reading_any_pr(self):
        orig = g.required_approvals
        g.required_approvals = lambda *a: (_ for _ in ()).throw(RuntimeError("404"))
        self.addCleanup(lambda: setattr(g, "required_approvals", orig))
        self.assertEqual(g.main(["--repo", "o/r", "--shared-login", SHARED[0], "7"]), 2)

    def test_a_branch_with_no_pull_request_rule_exits_2_never_MET(self):
        """The bug (yixuan-ag2, #4055): `docs/worker-pool-design` has no
        `pull_request` rule, so a zero-approval PR based there used to print
        "bar met, nobody to recruit" -- rc 0, the same as a real MET. Now it
        must refuse to answer rather than fabricate a satisfied bar, and it
        must do so WITHOUT ever reading a PR's reviews (nothing to score
        against).

        keweichen (#4055): asserting only `rc == 2` cannot tell this apart
        from the generic per-PR `except Exception` handler a few lines down
        also returning 2 -- deleting the `if required is None` branch
        entirely still passes that assertion, because the stubbed reviews
        call then raises and is caught there instead. Assert the reviews
        endpoint was never called, which only the correct branch satisfies.
        """
        self._stub([{"type": "deletion", "parameters": {}}],
                   {"7": RuntimeError("must not be called")})
        rc = g.main(["--repo", "o/r", "--shared-login", SHARED[0],
                     "--branch", "docs/worker-pool-design", "7"])
        self.assertEqual(rc, 2)
        self.assertFalse(any("pulls/7/reviews" in c for c in self.gh_calls),
                         f"reviews were fetched despite no bar: {self.gh_calls}")

    def test_repeatable_shared_login_reaches_classify(self):
        self._stub(self.RULES, {"7": [rv(SHARED[0], "APPROVED"), rv(SHARED[1], "APPROVED")]})
        rc = g.main(["--repo", "o/r", "--shared-login", SHARED[0],
                     "--shared-login", SHARED[1], "7"])
        self.assertEqual(rc, 0, "the bar is met, so it is thin -- not a recruit")

    # --- eligibility wiring end-to-end (keweichen, #4055) -------------------
    def test_an_ineligible_approver_is_excluded_from_the_live_verdict(self):
        self._stub(self.RULES, {"7": [rv("TustinOC", "APPROVED"), rv("a", "APPROVED")]})
        g.is_collaborator = lambda repo, login: login != "TustinOC"
        rc = g.main(["--repo", "o/r", "--shared-login", SHARED[0], "7"])
        self.assertEqual(rc, 1, "one real approval, short of the bar of 2")

    def test_undetermined_eligibility_is_cannot_answer_not_a_guess(self):
        """#7's undetermined approver refuses (worst code 2); #8 is a
        genuine RECRUIT (code 1) and must still run -- main() keeps going
        and reports the worst code rather than aborting the whole pass."""
        self._stub(self.RULES, {"7": [rv("a", "APPROVED"), rv("b", "APPROVED")],
                                 "8": [rv("c", "APPROVED")]})
        g.is_collaborator = lambda repo, login: None if login == "b" else True
        rc = g.main(["--repo", "o/r", "--shared-login", SHARED[0], "7", "8"])
        self.assertEqual(rc, 2)
        self.assertIn("repos/o/r/pulls/8/reviews", self.gh_calls,
                      "an undetermined approver on #7 must not abort #8")


if __name__ == "__main__":
    unittest.main(verbosity=1)
