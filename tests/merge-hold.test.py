#!/usr/bin/env python3
"""hooks/_merge_hold.py: an announced re-read by a reviewer with standing holds a PR until
they answer or it expires; anything unreadable is UNKNOWN. GitHub is a stub that answers the
one GraphQL query; the hold file lives in a temp dir.

Run:  python3 tests/merge-hold.test.py
"""
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "src"))
import sutando_config  # noqa: E402

_spec = importlib.util.spec_from_file_location("_merge_hold", REPO / "hooks" / "_merge_hold.py")
mh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mh)

R, N = "o/r", 6300
COMMIT = "2026-11-07T10:00:00Z"
NOW = mh._ts("2026-11-07T12:30:00Z")
AUTHOR = "pat"
TARGET = {"repo": R, "pr": N, "node": None}


def comment(who, at, body, assoc="NONE", typename="User"):
    return {"author": {"__typename": typename, "login": who}, "authorAssociation": assoc, "createdAt": at,
            "body": body, "url": f"https://x/c/{who}/{at}"}


def review(who, at, body="", assoc="NONE"):
    return {"author": {"__typename": "User", "login": who}, "authorAssociation": assoc, "submittedAt": at,
            "body": body, "url": f"https://x/r/{who}/{at}"}


class FakeGh:
    """Answers the module's one GraphQL query; records every call."""

    def __init__(self, comments=(), reviews=(), requested=("alice",), fail=False, more_comments=False):
        self.comments, self.reviews, self.requested = list(comments), list(reviews), requested
        self.fail, self.more_comments, self.calls = fail, more_comments, []

    def pull(self):
        return {"number": N, "repository": {"nameWithOwner": R}, "author": {"__typename": "User", "login": AUTHOR},
                "commits": {"nodes": [{"commit": {"committedDate": COMMIT}}]},
                "reviewRequests": {"nodes": [{"requestedReviewer": {"login": u}} for u in self.requested]
                                   + [{"requestedReviewer": {}}]},
                "reviews": {"pageInfo": {"hasPreviousPage": False}, "nodes": self.reviews},
                "comments": {"pageInfo": {"hasPreviousPage": self.more_comments}, "nodes": self.comments}}

    def __call__(self, args):
        self.calls.append(args)
        if self.fail:
            raise mh.GhError("gh api graphql: HTTP 502")
        if any(a.startswith("id=") for a in args):
            return json.dumps({"data": {"node": self.pull()}})
        return json.dumps({"data": {"repository": {"pullRequest": self.pull()}}})


class Policy(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.state = self.d / "merge-holds.json"

    def check(self, gh, now=NOW, ttl=4, target=TARGET):
        return mh.check(target, self.state, ttl, gh=gh, now=now)

    def status(self, *a, **k):
        return self.check(*a, **k)["status"]

    def test_no_activity_is_clear(self):
        self.assertEqual(self.status(FakeGh()), mh.CLEAR)

    def test_one_graphql_call_per_check(self):
        gh = FakeGh([comment("alice", "2026-11-07T11:00:00Z", "re-reading")])
        self.check(gh)
        self.assertEqual(len(gh.calls), 1)
        self.assertEqual(gh.calls[0][:2], ["api", "graphql"])

    def test_announced_reread_holds_and_names_who_when_where_expiry(self):
        v = self.check(FakeGh([comment("alice", "2026-11-07T11:00:00Z", "Thanks, re-reading the diff now.")]))
        self.assertEqual(v["status"], mh.HOLD)
        self.assertEqual(v["holds"], ["HOLD o/r#6300: alice at 2026-11-07T11:00:00Z (comment, expires "
                                      "2026-11-07T15:00:00Z) https://x/c/alice/2026-11-07T11:00:00Z"])

    def test_each_announcement_phrase_holds(self):
        for body in ("re-read pending", "rereading", "reading it again", "will re-review", "taking another look",
                     "hold merge please", "do not merge", "don't merge yet", "please wait",
                     "I'm re-reading the migration", "will re-read tonight", "Approved, but re-reading part 2"):
            self.assertEqual(self.status(FakeGh([comment("alice", "2026-11-07T11:00:00Z", body)])), mh.HOLD, body)

    def test_a_reread_named_as_done_or_quoted_is_not_an_announcement(self):
        for body in ("Approving. The owner approved this head after re-reading it.", "I re-read the diff; fine.",
                     "Having re-read it, no findings.", "> re-reading now\n\nThanks for the heads-up."):
            self.assertEqual(self.status(FakeGh([comment("alice", "2026-11-07T11:00:00Z", body)])), mh.CLEAR, body)

    def test_pr_author_cannot_hold_their_own_pr(self):
        gh = FakeGh([comment(AUTHOR, "2026-11-07T11:00:00Z", "please wait, re-reading", "OWNER")], requested=[AUTHOR])
        self.assertEqual(self.status(gh), mh.CLEAR)

    def test_a_login_without_standing_cannot_hold(self):
        for assoc in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"):
            gh = FakeGh([comment("drive-by", "2026-11-07T11:00:00Z", "do not merge!!", assoc)])
            self.assertEqual(self.status(gh), mh.CLEAR, assoc)

    def test_standing_from_requested_reviewer_past_review_or_association(self):
        self.assertEqual(self.status(FakeGh([comment("alice", "2026-11-07T11:00:00Z", "re-reading")])), mh.HOLD)
        for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
            gh = FakeGh([comment("carol", "2026-11-07T11:00:00Z", "re-reading", assoc)])
            self.assertEqual(self.status(gh), mh.HOLD, assoc)
        gh = FakeGh([comment("erin", "2026-11-07T11:00:00Z", "re-reading")],
                    [review("erin", "2026-11-06T09:00:00Z", "first pass")])
        self.assertEqual(self.status(gh), mh.HOLD)

    def test_bots_are_ignored(self):
        for c in (comment("github-actions", "2026-11-07T11:00:00Z", "Please wait for CI", "MEMBER", "Bot"),
                  comment("renovate[bot]", "2026-11-07T11:00:00Z", "please wait", "MEMBER")):
            self.assertEqual(self.status(FakeGh([c])), mh.CLEAR)

    def test_same_author_newer_review_of_any_state_resolves(self):
        gh = FakeGh([comment("alice", "2026-11-07T11:00:00Z", "Will re-review after lunch")],
                    [review("alice", "2026-11-07T12:00:00Z", "two nits")])
        self.assertEqual(self.status(gh), mh.CLEAR)

    def test_same_author_lgtm_comment_resolves(self):
        gh = FakeGh([comment("alice", "2026-11-07T11:00:00Z", "do not merge yet"),
                     comment("Alice", "2026-11-07T11:40:00Z", "LGTM, no further findings")])
        self.assertEqual(self.status(gh), mh.CLEAR)

    def test_another_authors_lgtm_does_not_resolve(self):
        gh = FakeGh([comment("alice", "2026-11-07T11:00:00Z", "please wait, taking another look"),
                     comment("bob", "2026-11-07T11:30:00Z", "LGTM, clear to merge", "OWNER")])
        self.assertEqual(self.status(gh), mh.HOLD)

    def test_a_newer_comment_that_announces_again_does_not_resolve(self):
        gh = FakeGh([comment("alice", "2026-11-07T11:00:00Z", "re-reading"),
                     comment("alice", "2026-11-07T11:30:00Z", "done with part 1, still re-reading part 2")])
        self.assertEqual(self.status(gh), mh.HOLD)

    def test_announcement_before_the_head_commit_is_ignored(self):
        self.assertEqual(self.status(FakeGh([comment("alice", "2026-11-07T09:00:00Z", "re-reading")])), mh.CLEAR)

    def test_review_body_announcement_holds(self):
        v = self.check(FakeGh([], [review("carol", "2026-11-07T11:00:00Z", "Shape is fine; re-reading the migration.")]))
        self.assertEqual(v["status"], mh.HOLD)
        self.assertIn("(review,", v["holds"][0])

    def test_a_hold_expires_after_the_ttl_as_an_advisory(self):
        gh = FakeGh([comment("alice", "2026-11-07T11:00:00Z", "re-reading")])
        self.assertEqual(self.status(gh, now=mh._ts("2026-11-07T14:59:00Z")), mh.HOLD)
        v = self.check(gh, now=mh._ts("2026-11-07T15:01:00Z"))
        self.assertEqual(v["status"], mh.CLEAR)
        self.assertEqual(v["advisories"], ["EXPIRED hold by alice announced 2026-11-07T11:00:00Z (4h01m old); "
                                           "re-ask them https://x/c/alice/2026-11-07T11:00:00Z"])
        self.assertEqual(self.status(gh, now=mh._ts("2026-11-07T15:01:00Z"), ttl=8), mh.HOLD)

    def test_ttl_comes_from_sutando_config(self):
        with tempfile.TemporaryDirectory() as d:
            root, cfg = Path(d), Path(d) / "sutando.config.json"
            for text, want in ((json.dumps({"env": {"MERGE_HOLD_TTL_HOURS": "1.5"}}), 1.5),
                               ("{}", mh.DEFAULT_TTL_HOURS)):
                cfg.write_text(text)
                sutando_config._reset_cache_for_tests()
                self.assertEqual(mh.ttl_hours(root), want)
            for bad in ("soon", "0", "-2"):
                cfg.write_text(json.dumps({"env": {"MERGE_HOLD_TTL_HOURS": bad}}))
                sutando_config._reset_cache_for_tests()
                with self.assertRaises(mh.StateError):
                    mh.ttl_hours(root)
        sutando_config._reset_cache_for_tests()

    def test_room_hold_add_holds_then_clear_releases(self):
        mh.add_hold(self.state, R, N, "dave", "said re-reading in a room")
        v = self.check(FakeGh(), now=None)
        self.assertEqual(v["status"], mh.HOLD)
        self.assertIn("dave", v["holds"][0])
        self.assertIn("(room hold,", v["holds"][0])
        self.assertEqual(mh.clear_hold(self.state, R, N, "dave"), 1)
        self.assertEqual(self.status(FakeGh(), now=None), mh.CLEAR)

    def test_room_hold_for_another_pr_does_not_hold(self):
        mh.add_hold(self.state, R, N + 1, "dave")
        self.assertEqual(self.status(FakeGh(), now=None), mh.CLEAR)

    def test_room_hold_resolved_by_a_newer_github_review(self):
        mh.add_hold(self.state, R, N, "alice", "re-reading")
        self.assertEqual(self.status(FakeGh([], [review("alice", "2099-01-01T00:00:00Z")]), now=None), mh.CLEAR)

    def test_room_hold_not_resolved_by_an_older_review(self):
        mh.add_hold(self.state, R, N, "alice", "re-reading")
        self.assertEqual(self.status(FakeGh([], [review("alice", "2026-11-07T10:30:00Z")]), now=None), mh.HOLD)

    def test_room_hold_expires_at_the_ttl_or_its_own_expires_at(self):
        mh.add_hold(self.state, R, N, "dave", "re-reading")
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5)
        v = self.check(FakeGh(), now=later)
        self.assertEqual(v["status"], mh.CLEAR)
        self.assertTrue(v["advisories"][0].startswith("EXPIRED hold by dave"))
        mh.add_hold(self.state, R, N, "dave", "long read",
                    expires_at=(later + dt.timedelta(hours=1)).strftime(mh.STAMP))
        self.assertEqual(self.status(FakeGh(), now=later), mh.HOLD)

    def test_node_target_resolves_repo_and_number_in_the_same_call(self):
        gh = FakeGh([comment("alice", "2026-11-07T11:00:00Z", "re-reading")])
        v = self.check(gh, target={"repo": None, "pr": None, "node": "PR_kw1"})
        self.assertEqual((v["status"], v["label"], len(gh.calls)), (mh.HOLD, "o/r#6300", 1))

    def test_gh_failure_is_unknown(self):
        v = self.check(FakeGh(fail=True))
        self.assertEqual(v["status"], mh.UNKNOWN)
        self.assertIn("502", v["reason"])

    def test_malformed_gh_response_is_unknown(self):
        for out in ("{not json", "{}", json.dumps({"data": {"repository": {"pullRequest": None}}})):
            self.assertEqual(self.status(lambda a, out=out: out), mh.UNKNOWN, out)

    def test_truncated_history_is_unknown(self):
        gh = FakeGh([comment("x", "2026-11-07T11:00:00Z", "hi")], more_comments=True)
        self.assertEqual(self.status(gh), mh.UNKNOWN)

    def test_corrupt_state_is_unknown(self):
        for text in ("{not json", '{"repo": "o/r"}', '[{"repo": "o/r"}]',
                     '[{"repo":"o/r","pr":1,"who":"a","said_at":"x"}]'):
            self.state.write_text(text)
            self.assertEqual(self.status(FakeGh()), mh.UNKNOWN, text)

    def test_bad_repo_is_unknown(self):
        self.assertEqual(self.status(FakeGh(), target={"repo": "not a repo", "pr": 1, "node": None}), mh.UNKNOWN)

    def test_add_hold_refuses_to_overwrite_a_corrupt_state_file(self):
        self.state.write_text("{not json")
        with self.assertRaises(mh.StateError):
            mh.add_hold(self.state, R, N, "dave")
        self.assertEqual(self.state.read_text(), "{not json")

    def test_state_paths_come_from_the_workspace_helper(self):
        ws = mh.workspace(REPO)
        self.assertEqual(mh.state_path(REPO), ws / "state" / "merge-holds.json")
        self.assertEqual(mh.override_log_path(REPO), ws / "state" / "guards" / "merge_hold_overrides.jsonl")

    def test_run_gh_has_a_five_second_budget(self):
        self.assertEqual(mh.GH_TIMEOUT, 5)
        seen = {}

        def slow(*a, **k):
            seen.update(k)
            raise subprocess.TimeoutExpired(a[0], k["timeout"])

        orig, mh.subprocess.run = mh.subprocess.run, slow
        try:
            with self.assertRaises(mh.GhError):
                mh.run_gh(["api", "graphql"])
        finally:
            mh.subprocess.run = orig
        self.assertEqual(seen["timeout"], 5)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def run_cli(self, *args):
        p = subprocess.run([sys.executable, str(REPO / "hooks" / "_merge_hold.py"), R, str(N),
                            "--state-file", str(self.d / "h.json"), *args],
                           capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout

    def test_add_and_clear_hold(self):
        self.assertEqual(self.run_cli("--add-hold", "--who", "dave", "--note", "n")[0], 0)
        self.assertEqual(json.loads((self.d / "h.json").read_text())[0]["who"], "dave")
        rc, out = self.run_cli("--clear-hold", "--who", "dave")
        self.assertEqual((rc, "CLEARED 1" in out), (0, True))

    def test_add_hold_requires_who(self):
        self.assertEqual(self.run_cli("--add-hold")[0], 2)

    def test_corrupt_state_file_is_unknown(self):
        (self.d / "h.json").write_text("{nope")
        self.assertEqual(self.run_cli("--add-hold", "--who", "dave")[0], 2)
        self.assertEqual((self.d / "h.json").read_text(), "{nope")


if __name__ == "__main__":
    os.environ.pop("MERGE_HOLD_TTL_HOURS", None)
    unittest.main(verbosity=1)
