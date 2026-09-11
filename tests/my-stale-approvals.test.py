#!/usr/bin/env python3
"""The two measurements this script exists to get right, both polarities.

SCOPE: a PR carrying your approval must be found whether or not you are also a
requested reviewer. Filtering by review-requests is the mistake that made a
by-hand scan report 3 of 19 as the total.

STALENESS: compare against the newest AUTHORED commit. A base merge moves the
head without anyone writing code, so a scan keyed on the head alone reports
approvals stale that are not — and a checker that cries wolf gets ignored.

Every positive is paired with its negative: found-when-stale AND absent-when-
fresh, decisive-when-uncontested AND not-decisive-when-someone-else-blocks.

Run: python3 tests/my-stale-approvals.test.py   (stdlib only, no network)
"""
import contextlib
import importlib.util
import io
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "my-stale-approvals.py"
ME = "me"


def _load():
    spec = importlib.util.spec_from_file_location("_msa", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _commit(date, parents=1):
    return {"sha": "x" * 40, "parents": [{}] * parents,
            "commit": {"committer": {"date": date}}}


def _review(login, state, at, assoc="COLLABORATOR"):
    return {"user": {"login": login}, "state": state,
            "submitted_at": at, "author_association": assoc}


class Fake:
    """Stands in for the gh CLI so the suite never touches the network."""

    def __init__(self, prs, reviews, commits):
        self.prs, self.reviews, self.commits = prs, reviews, commits

    def __call__(self, *args, default=None):
        if args[0] == "pr" and args[1] == "list":
            return self.prs
        joined = " ".join(args)
        for num in list(self.reviews) + list(self.commits):
            if f"/pulls/{num}/reviews" in joined:
                return self.reviews.get(num, [])
            if f"/pulls/{num}/commits" in joined:
                return self.commits.get(num, [])
        return default


class StaleApprovals(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _scan(self, prs, reviews, commits, bar=2):
        rows, _reach, _capped = self._scan2(prs, reviews, commits, bar)
        return rows

    def _scan2(self, prs, reviews, commits, bar=2):
        with patch.object(self.mod, "gh_json", Fake(prs, reviews, commits)):
            return self.mod.scan("o/r", ME, bar)

    def _pr(self, n, author="peer", draft=False, base="main"):
        return {"number": n, "title": f"pr {n}", "author": {"login": author},
                "isDraft": draft, "baseRefName": base}

    # --- SCOPE ------------------------------------------------------------

    def test_found_even_though_no_review_was_requested_of_me(self):
        """The defect this script exists to prevent: the review-request list is
        not the population. Nothing here mentions reviewRequests at all."""
        rows = self._scan([self._pr(1)],
                          {1: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z")]},
                          {1: [_commit("2026-01-02T00:00:00Z")]})
        self.assertEqual([r["number"] for r in rows], [1])
        self.assertEqual(rows[0]["commits_after"], 1)

    # --- STALENESS --------------------------------------------------------

    def test_CONTROL_an_approval_at_the_newest_authored_commit_is_not_stale(self):
        rows = self._scan([self._pr(2)],
                          {2: [_review(ME, "APPROVED", "2026-01-03T00:00:00Z")]},
                          {2: [_commit("2026-01-02T00:00:00Z")]})
        self.assertEqual(rows, [])

    def test_a_base_MERGE_after_my_approval_does_not_make_it_stale(self):
        """A merge commit moves the head with nobody writing code. Keying on the
        head alone reports this as stale, which is the false positive."""
        rows = self._scan([self._pr(3)],
                          {3: [_review(ME, "APPROVED", "2026-01-03T00:00:00Z")]},
                          {3: [_commit("2026-01-02T00:00:00Z"),
                               _commit("2026-01-04T00:00:00Z", parents=2)]})
        self.assertEqual(rows, [], "a base merge is not authored work")

    def test_my_CHANGES_REQUESTED_is_not_a_stale_approval(self):
        rows = self._scan([self._pr(4)],
                          {4: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z"),
                               _review(ME, "CHANGES_REQUESTED", "2026-01-02T00:00:00Z")]},
                          {4: [_commit("2026-01-03T00:00:00Z")]})
        self.assertEqual(rows, [], "latest review wins, and a block is not an authorisation")

    def test_my_own_PR_is_skipped(self):
        rows = self._scan([self._pr(5, author=ME)],
                          {5: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z")]},
                          {5: [_commit("2026-01-02T00:00:00Z")]})
        self.assertEqual(rows, [])

    # --- DECISIVE ---------------------------------------------------------

    def test_decisive_when_nobody_else_blocks_and_the_bar_is_within_reach(self):
        rows = self._scan([self._pr(6)],
                          {6: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z")]},
                          {6: [_commit("2026-01-02T00:00:00Z")]})
        self.assertTrue(rows[0]["decisive"])
        self.assertEqual(rows[0]["qualifying_approvals"], 1)

    def test_NOT_decisive_when_someone_else_holds_changes_requested(self):
        rows = self._scan([self._pr(7)],
                          {7: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z"),
                               _review("other", "CHANGES_REQUESTED", "2026-01-01T00:00:00Z")]},
                          {7: [_commit("2026-01-02T00:00:00Z")]})
        self.assertFalse(rows[0]["decisive"])
        self.assertEqual(rows[0]["blocked_by_others"], ["other"])

    def test_a_CONTRIBUTOR_approval_does_not_count_toward_the_bar(self):
        """Only COLLABORATOR/MEMBER/OWNER count at the gate, so counting names
        rather than associations overstates how close a PR is."""
        rows = self._scan([self._pr(8)],
                          {8: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z"),
                               _review("drive-by", "APPROVED", "2026-01-01T00:00:00Z",
                                       assoc="CONTRIBUTOR")]},
                          {8: [_commit("2026-01-02T00:00:00Z")]})
        self.assertEqual(rows[0]["qualifying_approvals"], 1, "the CONTRIBUTOR must not be counted")

    def test_a_commit_AT_the_approval_timestamp_does_not_count_as_after(self):
        """Pins the boundary so widening `>` to `>=` cannot pass silently. The
        commit is not 'after' the review that covered it at the same instant."""
        rows = self._scan([self._pr(11)],
                          {11: [_review(ME, "APPROVED", "2026-01-02T00:00:00Z")]},
                          {11: [_commit("2026-01-02T00:00:00Z"),
                                _commit("2026-01-03T00:00:00Z")]})
        self.assertEqual(rows[0]["commits_after"], 1,
                         "only the strictly-later commit is after the approval")

    def test_NOT_decisive_at_the_PRODUCTION_bar_when_my_own_approval_does_not_count(self):
        """The bar clause has to discriminate at bar=2, the bar this fleet uses.
        It does, in exactly one place: `qualifying` counts my own review, so it is
        >= 1 whenever my association gates — and 0 when it does not, which is the
        only case where `not blockers` and the real predicate disagree here."""
        rows = self._scan([self._pr(22)],
                          {22: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z",
                                        assoc="CONTRIBUTOR")]},
                          {22: [_commit("2026-01-02T00:00:00Z")]})
        self.assertEqual(rows[0]["qualifying_approvals"], 0)
        self.assertEqual(rows[0]["blocked_by_others"], [])
        self.assertFalse(rows[0]["decisive"],
                         "a vote that does not count at the gate cannot be the decisive one")

    def test_NOT_decisive_when_the_bar_is_out_of_reach(self):
        """An unblocked PR is not automatically decisive: the tally must be at or
        one short of the bar. At bar=3 with one approval it is two short, so no
        merge is near and the stale tick is not urgent."""
        rows = self._scan([self._pr(21)],
                          {21: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z")]},
                          {21: [_commit("2026-01-05T00:00:00Z")]}, bar=3)
        self.assertEqual(rows[0]["qualifying_approvals"], 1)
        self.assertFalse(rows[0]["decisive"],
                         "no other blockers is not sufficient; the bar must be reachable")

    def test_decisive_AT_one_short_of_the_bar_even_though_my_vote_cannot_carry_it(self):
        """The cell where the two readings of `decisive` disagree, pinned to the
        one this tool means. At bar=2 with mine the only qualifying approval, my
        re-review cannot land the PR — someone else's approval is the missing one.
        It is still decisive here, because `dismiss_stale_reviews_on_push` is false
        on this repo, so my stale tick is already half of whatever merges it.
        Raised by @yixuan-ag2, who read the field name against the code."""
        rows = self._scan([self._pr(23)],
                          {23: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z")]},
                          {23: [_commit("2026-01-02T00:00:00Z")]}, bar=2)
        self.assertEqual(rows[0]["qualifying_approvals"], 1)  # exactly bar - 1
        self.assertTrue(rows[0]["decisive"])

    def test_decisive_sorts_first(self):
        rows = self._scan(
            [self._pr(9), self._pr(10)],
            {9: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z"),
                 _review("other", "CHANGES_REQUESTED", "2026-01-01T00:00:00Z")],
             10: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z")]},
            {9: [_commit("2026-01-02T00:00:00Z")],
             10: [_commit("2026-01-02T00:00:00Z")]})
        self.assertEqual([r["number"] for r in rows], [10, 9])


class Scope(unittest.TestCase):
    """The second axis: a single-repo scan reported as a total.

    Filtering within a repo and scoping across repos are different defects.
    Fixing the first and calling the scope fixed is what this class pins.
    """

    def setUp(self):
        self.mod = _load()

    def test_repos_reviewed_finds_every_repo_not_just_the_main_one(self):
        search = {"items": [
            {"repository_url": "https://api.github.com/repos/o/main-repo"},
            {"repository_url": "https://api.github.com/repos/o/main-repo"},
            {"repository_url": "https://api.github.com/repos/other/elsewhere"},
        ]}
        with patch.object(self.mod, "gh_json", lambda *a, **k: search):
            repos, _cov = self.mod.repos_reviewed(ME)
            self.assertEqual(repos, ["o/main-repo", "other/elsewhere"])

    def test_repos_reviewed_is_empty_when_the_search_returns_nothing(self):
        with patch.object(self.mod, "gh_json", lambda *a, **k: {"items": []}):
            repos, _cov = self.mod.repos_reviewed(ME)
            self.assertEqual(repos, [])

    def test_reach_counts_PRs_carrying_my_review_so_a_zero_is_testable(self):
        """A 0 from a repo holding none of your approvals is untestable, not a
        measurement — positive controls inside a reachable set never test reach."""
        s2 = StaleApprovals("test_my_own_PR_is_skipped")
        s2.mod = self.mod
        rows, reach, _capped = s2._scan2(
            [s2._pr(1)],
            {1: [_review(ME, "APPROVED", "2026-01-03T00:00:00Z")]},
            {1: [_commit("2026-01-02T00:00:00Z")]})
        self.assertEqual(rows, [], "approval is at head, so not stale")
        self.assertEqual(reach, 1, "but the scan DID see my review here")

    def test_reach_is_zero_when_no_PR_carries_my_review(self):
        s2 = StaleApprovals("test_my_own_PR_is_skipped")
        s2.mod = self.mod
        rows, reach, _capped = s2._scan2(
            [s2._pr(1)],
            {1: [_review("someone-else", "APPROVED", "2026-01-01T00:00:00Z")]},
            {1: [_commit("2026-01-02T00:00:00Z")]})
        self.assertEqual((rows, reach), ([], 0))


class MultiRepoDefault(unittest.TestCase):
    """main() must ACTUALLY scan every discovered repo when --repo is omitted.

    Mutation-found gap (yixuan-ag2, at fd96c7867): reverting discovery to a
    single hardcoded repo left all 13 tests green, so the repo-agnostic
    behaviour was a decision rather than an invariant — and it is the exact
    defect the change exists to fix. Testing repos_reviewed() in isolation does
    not pin that main() calls it.
    """

    def setUp(self):
        self.mod = _load()

    def _drive(self, argv):
        """Two repos, one stale approval each, distinguishable by PR number."""
        per_repo = {
            "o/alpha": (101, "2026-01-01T00:00:00Z"),
            "o/beta": (202, "2026-01-01T00:00:00Z"),
        }
        search = {"items": [
            {"repository_url": "https://api.github.com/repos/o/alpha"},
            {"repository_url": "https://api.github.com/repos/o/beta"},
        ]}
        login_in_query = ME

        def fake(*args, default=None):
            joined = " ".join(str(a) for a in args)
            if "search/issues" in joined:
                # A stub that answers regardless of the query pins the plumbing
                # and not the question asked; the query is on the far side.
                assert "reviewed-by" in joined, \
                    f"discovery query lost its reviewed-by filter: {joined}"
                # Parse the value and compare EXACTLY. Any substring form lets a
                # longer login pass on a prefix: "reviewed-by:me" is inside "merge-bot".
                got = re.search(r"reviewed-by:([A-Za-z0-9._-]+)", joined)
                assert got and got.group(1) == login_in_query, \
                    f"discovery query names {got and got.group(1)!r}, not the login: {joined}"
                return search
            if args[0] == "api" and joined.endswith("user"):
                return {"login": ME}
            for repo, (num, _at) in per_repo.items():
                if f"--repo {repo}" in joined or f"repos/{repo}/" in joined:
                    if args[0] == "pr" and args[1] == "list":
                        return [{"number": num, "title": f"t{num}",
                                 "author": {"login": "peer"}, "isDraft": False,
                                 "baseRefName": "main"}]
                    if f"/pulls/{num}/reviews" in joined:
                        return [_review(ME, "APPROVED", per_repo[repo][1])]
                    if f"/pulls/{num}/commits" in joined:
                        return [_commit("2026-01-02T00:00:00Z")]
            return default

        out = io.StringIO()
        with patch.object(self.mod, "gh_json", fake), \
             patch.object(self.mod.sys, "argv", ["my-stale-approvals.py"] + argv), \
             contextlib.redirect_stdout(out):
            rc = self.mod.main()
        return rc, out.getvalue()

    def test_omitting_repo_scans_EVERY_discovered_repo(self):
        rc, out = self._drive(["--login", ME])
        self.assertEqual(rc, 0)
        self.assertIn("o/alpha#101", out)
        self.assertIn("o/beta#202", out, "the second repo must be scanned, not just the first")
        self.assertIn("2 stale approval(s)", out)
        self.assertIn("across 2 repo(s)", out)

    def test_CONTROL_an_explicit_repo_scans_only_that_one(self):
        rc, out = self._drive(["--login", ME, "--repo", "o/alpha"])
        self.assertEqual(rc, 0)
        self.assertIn("o/alpha#101", out)
        self.assertNotIn("o/beta", out, "--repo must stay a narrowing flag")
        self.assertIn("across 1 repo(s)", out)

    def test_the_scope_line_names_every_repo_scanned(self):
        _rc, out = self._drive(["--login", ME])
        scope = out.split("scope:", 1)[1]
        self.assertIn("o/alpha", scope)
        self.assertIn("o/beta", scope)


class Plumbing(unittest.TestCase):
    """The gh wrapper and main()'s exits. Every one of these is a path a real
    run takes on a bad day, and all were unreached: the suite patches gh_json,
    so the thing that actually shells out was never itself exercised."""

    def setUp(self):
        self.mod = _load()

    class _Proc:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    def test_gh_json_parses_a_successful_call(self):
        with patch.object(self.mod.subprocess, "run",
                          lambda *a, **k: self._Proc(0, '{"x": 1}')):
            self.assertEqual(self.mod.gh_json("api", "x"), {"x": 1})

    def test_gh_json_returns_the_default_on_a_nonzero_exit(self):
        with patch.object(self.mod.subprocess, "run",
                          lambda *a, **k: self._Proc(1, "")):
            self.assertEqual(self.mod.gh_json("api", "x", default="D"), "D")

    def test_gh_json_returns_the_default_on_UNPARSEABLE_output(self):
        """rc 0 with garbage is the shape that would otherwise raise mid-scan."""
        with patch.object(self.mod.subprocess, "run",
                          lambda *a, **k: self._Proc(0, "not json")):
            self.assertEqual(self.mod.gh_json("api", "x", default=[]), [])

    def test_current_login_reads_the_authenticated_user(self):
        with patch.object(self.mod, "gh_json", lambda *a, **k: {"login": "somebody"}):
            self.assertEqual(self.mod.current_login(), "somebody")

    def test_current_login_is_None_when_gh_cannot_answer(self):
        with patch.object(self.mod, "gh_json", lambda *a, **k: None):
            self.assertIsNone(self.mod.current_login())

    def _main(self, argv, gh):
        err, out = io.StringIO(), io.StringIO()
        with patch.object(self.mod, "gh_json", gh), \
             patch.object(self.mod.sys, "argv", ["my-stale-approvals.py"] + argv), \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            return self.mod.main(), out.getvalue(), err.getvalue()

    def test_an_unresolvable_login_exits_2_and_says_how_to_fix_it(self):
        rc, _out, err = self._main([], lambda *a, **k: None)
        self.assertEqual(rc, 2)
        self.assertIn("--login", err, "the refusal must name the flag that satisfies it")

    def test_no_discovered_repo_exits_0_and_says_so(self):
        rc, _out, err = self._main(["--login", ME], lambda *a, **k: {"items": []})
        self.assertEqual(rc, 0, "finding nothing to scan is not an error")
        self.assertIn("no repo", err)

    def test_json_output_carries_the_scope_so_it_is_not_lost_to_machines(self):
        rc, out, _err = self._main(["--login", ME, "--repo", "o/r", "--json"],
                                   lambda *a, **k: [] if a[0] == "pr" else {})
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["login"], ME)
        self.assertEqual([s["repo"] for s in payload["scanned"]], ["o/r"])

    def test_fail_on_decisive_exits_1_when_a_decisive_row_exists(self):
        def gh(*args, default=None):
            joined = " ".join(str(x) for x in args)
            if args[0] == "pr" and args[1] == "list":
                return [{"number": 7, "title": "t", "author": {"login": "peer"},
                         "isDraft": False, "baseRefName": "main"}]
            if "/reviews" in joined:
                return [_review(ME, "APPROVED", "2026-01-01T00:00:00Z")]
            if "/commits" in joined:
                return [_commit("2026-01-02T00:00:00Z")]
            return default
        rc, _out, _err = self._main(["--login", ME, "--repo", "o/r", "--fail-on-decisive"], gh)
        self.assertEqual(rc, 1, "opt-in gate must fail the build when it is armed")
        rc2, _o, _e = self._main(["--login", ME, "--repo", "o/r"], gh)
        self.assertEqual(rc2, 0, "CONTROL: without the flag it stays a report")


class DismissedClearsTheStance(unittest.TestCase):
    """GitHub counts only each author's latest review, and a DISMISSED one is no
    stance. Skipping DISMISSED rather than clearing resurrects the author's
    PREVIOUS review, which is wrong in both polarities — and both were live:

    on sonichi/sutando#3356 the old code reported a qingyun-wu approval that
    GitHub's own `latestReviews` does not list at all, and on #3327 / #3537 it
    listed a blocker GitHub no longer holds, hiding a DECISIVE row behind a
    phantom. There was no DISMISSED anywhere in this suite before now.
    """

    def setUp(self):
        self.mod = _load()

    def _scan(self, prs, reviews, commits, bar=2):
        with patch.object(self.mod, "gh_json", Fake(prs, reviews, commits)):
            return self.mod.scan("o/r", ME, bar)[0]

    def _pr(self, n, author="peer"):
        return {"number": n, "title": f"pr {n}", "author": {"login": author},
                "isDraft": False, "baseRefName": "main"}

    def test_my_APPROVED_then_DISMISSED_is_not_a_stale_approval(self):
        """The #3356 polarity: a row claiming an approval authorises unread code
        while GitHub counts no approval of mine at all."""
        rows = self._scan(
            [self._pr(1)],
            {1: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z"),
                 _review(ME, "DISMISSED", "2026-01-01T01:00:00Z")]},
            {1: [_commit("2026-01-02T00:00:00Z")]})
        self.assertEqual(rows, [])

    def test_CONTROL_without_the_dismissal_that_same_row_IS_reported(self):
        """Pins that the arm above fails for the dismissal and not because the
        fixture is inert."""
        rows = self._scan(
            [self._pr(1)],
            {1: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z")]},
            {1: [_commit("2026-01-02T00:00:00Z")]})
        self.assertEqual([r["number"] for r in rows], [1])

    def test_a_DISMISSED_block_is_not_a_blocker(self):
        """The #3327 polarity, and the dangerous one: a phantom blocker hides a
        DECISIVE row, which is the state this tool exists to surface."""
        rows = self._scan(
            [self._pr(2)],
            {2: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z"),
                 _review("other", "CHANGES_REQUESTED", "2026-01-01T02:00:00Z"),
                 _review("other", "DISMISSED", "2026-01-01T03:00:00Z"),
                 _review("third", "APPROVED", "2026-01-01T04:00:00Z")]},
            {2: [_commit("2026-01-02T00:00:00Z")]})
        self.assertEqual(rows[0]["blocked_by_others"], [])
        self.assertTrue(rows[0]["decisive"])

    def test_CONTROL_an_UNdismissed_block_still_blocks(self):
        rows = self._scan(
            [self._pr(2)],
            {2: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z"),
                 _review("other", "CHANGES_REQUESTED", "2026-01-01T02:00:00Z")]},
            {2: [_commit("2026-01-02T00:00:00Z")]})
        self.assertEqual(rows[0]["blocked_by_others"], ["other"])
        self.assertFalse(rows[0]["decisive"])

    def test_a_dismissal_BEFORE_a_later_review_does_not_erase_it(self):
        """Order matters: clearing must apply to the dismissal's position in the
        sequence, not to the author wholesale."""
        rows = self._scan(
            [self._pr(3)],
            {3: [_review(ME, "APPROVED", "2026-01-01T00:00:00Z"),
                 _review(ME, "DISMISSED", "2026-01-01T01:00:00Z"),
                 _review(ME, "APPROVED", "2026-01-01T02:00:00Z")]},
            {3: [_commit("2026-01-03T00:00:00Z")]})
        self.assertEqual([r["number"] for r in rows], [3])


class MergesAfterAreAnUnknownNotAZero(unittest.TestCase):
    """A conflict-resolving merge is a 2-parent commit whose resolution hunks
    were typed by a person. Staleness is still keyed on authored commits — that
    is deliberate and unchanged — but a merge after the approval must print as
    an unknown rather than vanishing into "nobody wrote anything"."""

    def setUp(self):
        self.mod = _load()

    def _pr(self, n):
        return {"number": n, "title": f"pr {n}", "author": {"login": "peer"},
                "isDraft": False, "baseRefName": "main"}

    def test_a_merge_after_the_approval_is_counted_separately(self):
        with patch.object(self.mod, "gh_json", Fake(
                [self._pr(1)],
                {1: [_review(ME, "APPROVED", "2026-01-02T00:00:00Z")]},
                {1: [_commit("2026-01-01T00:00:00Z"),
                     _commit("2026-01-03T00:00:00Z", parents=2),
                     _commit("2026-01-04T00:00:00Z", parents=2)]})):
            rows, _reach, _cap = self.mod.scan("o/r", ME, 2)
        # Not stale by authored commits, so nothing to report at all...
        self.assertEqual(rows, [])

    def test_merges_after_appear_on_a_row_that_IS_stale(self):
        with patch.object(self.mod, "gh_json", Fake(
                [self._pr(2)],
                {2: [_review(ME, "APPROVED", "2026-01-02T00:00:00Z")]},
                {2: [_commit("2026-01-03T00:00:00Z"),
                     _commit("2026-01-04T00:00:00Z", parents=2)]})):
            rows, _reach, _cap = self.mod.scan("o/r", ME, 2)
        self.assertEqual(rows[0]["commits_after"], 1)
        self.assertEqual(rows[0]["merges_after"], 1)

    def test_a_merge_BEFORE_the_approval_is_not_counted(self):
        with patch.object(self.mod, "gh_json", Fake(
                [self._pr(3)],
                {3: [_review(ME, "APPROVED", "2026-01-05T00:00:00Z")]},
                {3: [_commit("2026-01-04T00:00:00Z", parents=2),
                     _commit("2026-01-06T00:00:00Z")]})):
            rows, _reach, _cap = self.mod.scan("o/r", ME, 2)
        self.assertEqual(rows[0]["merges_after"], 0)


class SearchCeilingIsReportedNotAssumedAbsent(unittest.TestCase):
    """`repos_reviewed` read ONE page and never looked at total_count. Measured
    live: `is:pr is:open reviewed-by:qingyun-wu` -> total_count 134, items 100,
    so 34 PRs deciding the repo set were unseen and any repo appearing only in
    them was silently out of scope."""

    def setUp(self):
        self.mod = _load()

    def test_pages_are_concatenated(self):
        pages = [
            {"total_count": 3, "items": [
                {"repository_url": "https://api.github.com/repos/o/a"}]},
            {"total_count": 3, "items": [
                {"repository_url": "https://api.github.com/repos/o/b"},
                {"repository_url": "https://api.github.com/repos/o/a"}]},
        ]
        with patch.object(self.mod, "gh_json", lambda *a, **k: pages):
            repos, cov = self.mod.repos_reviewed(ME)
        self.assertEqual(repos, ["o/a", "o/b"])
        self.assertEqual(cov, {"total": 3, "seen": 3, "ok": True})

    def test_a_SHORTFALL_is_reported_rather_than_read_as_complete(self):
        page = {"total_count": 134, "items": [
            {"repository_url": "https://api.github.com/repos/o/a"}]}
        with patch.object(self.mod, "gh_json", lambda *a, **k: page):
            _repos, cov = self.mod.repos_reviewed(ME)
        self.assertLess(cov["seen"], cov["total"])

    def test_the_shortfall_reaches_the_OUTPUT_not_just_the_return_value(self):
        """A coverage number nobody prints cannot correct a reader."""
        page = {"total_count": 134, "items": [
            {"repository_url": "https://api.github.com/repos/o/a"}]}
        out = io.StringIO()
        with patch.object(self.mod, "gh_json", lambda *a, **k: page), \
                patch.object(self.mod, "current_login", lambda: ME), \
                patch.object(self.mod, "scan", lambda *a, **k: ([], 0, False)), \
                patch("sys.argv", ["x"]), contextlib.redirect_stdout(out):
            self.mod.main()
        self.assertIn("134", out.getvalue())
        self.assertRegex(out.getvalue(), r"reached 1 of 134")

    def test_a_FAILED_search_is_not_an_empty_one(self):
        """gh_json swallows a decode error into its default, so the two states
        arrive identical. Measured: --paginate without --slurp emits one JSON
        object per page, and a 2-page (134-result) search silently became 0
        repos while a 1-page (96-result) one was fine — a single-login check
        would have passed."""
        with patch.object(self.mod, "gh_json", lambda *a, **k: None):
            repos, cov = self.mod.repos_reviewed(ME)
        self.assertEqual(repos, [])
        self.assertFalse(cov["ok"])

    def test_a_GENUINELY_empty_search_is_reported_as_ok(self):
        with patch.object(self.mod, "gh_json",
                          lambda *a, **k: {"total_count": 0, "items": []}):
            repos, cov = self.mod.repos_reviewed(ME)
        self.assertEqual(repos, [])
        self.assertTrue(cov["ok"])

    def test_the_search_is_slurped_or_pagination_silently_empties_it(self):
        seen = {}
        def spy(*args, **k):
            seen["args"] = args
            return {"total_count": 0, "items": []}
        with patch.object(self.mod, "gh_json", spy):
            self.mod.repos_reviewed(ME)
        self.assertIn("--paginate", seen["args"])
        self.assertIn("--slurp", seen["args"])

    def test_the_PR_LIST_CAP_is_reported_too(self):
        """`gh pr list --limit 200` at the cap means the remainder is unseen."""
        prs = [{"number": i, "title": "t", "author": {"login": "peer"},
                "isDraft": True, "baseRefName": "main"}
               for i in range(self.mod.PR_LIST_LIMIT)]
        with patch.object(self.mod, "gh_json", Fake(prs, {}, {})):
            _rows, _reach, capped = self.mod.scan("o/r", ME, 2)
        self.assertTrue(capped)

    def test_CONTROL_below_the_cap_is_not_flagged(self):
        prs = [{"number": 1, "title": "t", "author": {"login": "peer"},
                "isDraft": True, "baseRefName": "main"}]
        with patch.object(self.mod, "gh_json", Fake(prs, {}, {})):
            _rows, _reach, capped = self.mod.scan("o/r", ME, 2)
        self.assertFalse(capped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
