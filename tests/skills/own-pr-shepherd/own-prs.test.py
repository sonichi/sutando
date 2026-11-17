#!/usr/bin/env python3
"""skills/own-pr-shepherd/scripts/own_prs.py: the classifier over fabricated PR nodes, search
paging to the end (the `--limit` trap) and the incomplete verdict, the smaller-page retry, the
quota floor, ownership by commit email, `approved` always going to the owner, the acted record
(locked) keyed on the last non-merge commit, the shared review-asks ledger, and the list CLI. No
network: GraphQL is a stub."""
import importlib.util
import contextlib
import io
import json
import os
import threading
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

_spec = importlib.util.spec_from_file_location(
    "own_prs", str(REPO / "skills" / "own-pr-shepherd" / "scripts" / "own_prs.py"))
op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(op)

HEAD = "h" * 40


OWNER = "owner@example.org"


def commit(oid, at="2026-11-14T10:00:00Z", merge=False, email=OWNER):
    return {"commit": {"oid": oid, "committedDate": at, "parents": {"totalCount": 2 if merge else 1},
                       "author": {"email": email}}}


def pr(number=1, decision="REVIEW_REQUIRED", mergeable="MERGEABLE", mss="BLOCKED", rollup="SUCCESS",
       contexts=(), reviews=(), requests=0, queued=False, draft=False, committed="2026-11-14T10:00:00Z",
       recent=None, email=OWNER):
    return {
        "recent": {"nodes": list(recent) if recent is not None else [commit(HEAD, committed, email=email)]},
        "number": number, "url": f"https://x/{number}", "isDraft": draft, "isInMergeQueue": queued,
        "headRefOid": HEAD, "baseRefName": "main", "reviewDecision": decision,
        "mergeable": mergeable, "mergeStateStatus": mss, "author": {"login": "owner"},
        "reviewRequests": {"totalCount": requests},
        "latestReviews": {"nodes": list(reviews)},
        "commits": {"nodes": [{"commit": {"oid": HEAD, "committedDate": committed,
                                          "statusCheckRollup": {"state": rollup,
                                                                "contexts": {"nodes": list(contexts)}}}}]},
    }


def merged_base(number=1, **kw):
    """A PR whose head is a base merge (update-branch) on top of the owner's commit "c1"."""
    p = pr(number, committed="2026-11-14T13:00:00Z",
           recent=[commit("c1", "2026-11-14T10:00:00Z"), commit(HEAD, "2026-11-14T13:00:00Z", merge=True)], **kw)
    return p


def review(state, oid="", at="2026-11-14T11:00:00Z", who="rev"):
    return {"state": state, "submittedAt": at, "author": {"login": who}, "commit": {"oid": oid}}


class Classify(unittest.TestCase):
    def test_changes_requested_on_the_current_head_is_a_fix(self):
        p = pr(decision="CHANGES_REQUESTED", reviews=[review("CHANGES_REQUESTED", oid=HEAD)])
        self.assertEqual(op.classify(p), ["changes_requested"])
        self.assertEqual(op.next_step("changes_requested"), "fix")

    def test_a_new_commit_after_the_request_is_a_re_solicit(self):
        p = pr(decision="CHANGES_REQUESTED", reviews=[review("CHANGES_REQUESTED", oid="old")],
               recent=[commit("old"), commit(HEAD)])
        self.assertEqual(op.classify(p), ["changes_addressed"])
        self.assertEqual(op.next_step("changes_addressed"), "re-solicit")

    def test_a_base_merge_alone_addresses_nothing(self):
        # gh pr update-branch moves the head with a merge commit only: the fix is still owed.
        p = pr(decision="CHANGES_REQUESTED", reviews=[review("CHANGES_REQUESTED", oid="old")],
               recent=[commit("old"), commit(HEAD, merge=True)])
        self.assertEqual(op.classify(p), ["changes_requested"])

    def test_every_blocking_review_must_be_followed_by_content(self):
        p = pr(decision="CHANGES_REQUESTED",
               reviews=[review("CHANGES_REQUESTED", oid="a", who="r1"),
                        review("CHANGES_REQUESTED", oid="b", who="r2")],
               recent=[commit("a"), commit("b"), commit(HEAD, merge=True)])
        self.assertEqual(op.classify(p), ["changes_requested"], "r2 has seen nothing new")

    def test_without_a_review_commit_the_dates_decide(self):
        recent = [commit("x", "2026-11-14T08:00:00Z"), commit(HEAD, "2026-11-14T10:00:00Z")]
        before = review("CHANGES_REQUESTED", at="2026-11-14T09:00:00Z")
        after = review("CHANGES_REQUESTED", at="2026-11-14T11:00:00Z")
        self.assertEqual(op.classify(pr(decision="CHANGES_REQUESTED", reviews=[before], recent=recent)),
                         ["changes_addressed"])
        self.assertEqual(op.classify(pr(decision="CHANGES_REQUESTED", reviews=[after], recent=recent)),
                         ["changes_requested"])

    def test_every_applicable_class_is_listed_primary_first(self):
        p = pr(decision="CHANGES_REQUESTED", mergeable="CONFLICTING", mss="DIRTY", rollup="FAILURE",
               reviews=[review("CHANGES_REQUESTED", oid=HEAD)],
               contexts=[{"__typename": "CheckRun", "name": "lint", "conclusion": "FAILURE"}])
        self.assertEqual(op.classify(p), ["changes_requested", "conflicting", "ci_red"])
        self.assertEqual(op.failing_checks(p), ["lint"])

    def test_red_ci_from_a_status_context(self):
        p = pr(requests=1, contexts=[{"__typename": "StatusContext", "context": "license/cla", "state": "ERROR"}])
        self.assertEqual(op.classify(p), ["ci_red"])

    def test_skipped_and_pending_checks_are_not_red(self):
        p = pr(requests=1, rollup="PENDING", contexts=[
            {"__typename": "CheckRun", "name": "a", "conclusion": "SKIPPED"},
            {"__typename": "CheckRun", "name": "b", "conclusion": None}])
        self.assertEqual(op.classify(p), ["waiting_review"])

    def test_behind_base(self):
        self.assertEqual(op.classify(pr(requests=1, mss="BEHIND")), ["behind"])

    def test_no_reviewer_and_no_review_needs_one(self):
        self.assertEqual(op.classify(pr()), ["needs_reviewer"])
        self.assertEqual(op.classify(pr(requests=1)), ["waiting_review"])
        self.assertEqual(op.classify(pr(reviews=[review("COMMENTED")])), ["waiting_review"])

    def test_the_authors_own_comment_is_not_a_review(self):
        self.assertEqual(op.classify(pr(reviews=[review("COMMENTED", who="owner")])), ["needs_reviewer"])

    def test_approved_always_goes_to_the_owner(self):
        p = pr(decision="APPROVED", mss="CLEAN")
        self.assertEqual(op.classify(p), ["approved"])
        self.assertEqual(op.next_step("approved"), "ask-owner")
        self.assertNotIn("enqueue", op.NEXT.values(), "no step of this pass merges")
        self.assertNotIn("enqueuePullRequest", Path(op.__file__).read_text())

    def test_approved_with_ci_running_waits(self):
        self.assertEqual(op.classify(pr(decision="APPROVED", rollup="PENDING")), ["approved_ci_pending"])

    def test_already_queued_is_left_alone(self):
        self.assertEqual(op.classify(pr(decision="APPROVED", queued=True)), ["queued"])

    def test_unknown_mergeability_is_rechecked(self):
        self.assertEqual(op.classify(pr(requests=1, mergeable="UNKNOWN", mss="UNKNOWN")), ["unknown"])


class Ownership(unittest.TestCase):
    def test_the_last_non_merge_commit_decides(self):
        mine = pr(recent=[commit("a", email="other@x"), commit(HEAD, email="Owner@Example.org")])
        self.assertTrue(op.is_mine(mine, OWNER))
        theirs = pr(recent=[commit("a", email=OWNER), commit("b", email="other@x"), commit(HEAD, merge=True)])
        self.assertFalse(op.is_mine(theirs, OWNER), "another agent's work under the same login")
        merged_in = pr(recent=[commit("a", email=OWNER), commit(HEAD, merge=True, email="other@x")])
        self.assertTrue(op.is_mine(merged_in, OWNER), "a merge commit decides nothing")
        self.assertFalse(op.is_mine(pr(recent=[]), OWNER))
        self.assertFalse(op.is_mine(mine, ""))


class Search(unittest.TestCase):
    def pages(self, chunks, total):
        calls = []

        def gql(_q, v):
            calls.append(v)
            i = len(calls) - 1
            return {"search": {"issueCount": total, "nodes": chunks[i],
                               "pageInfo": {"hasNextPage": i + 1 < len(chunks), "endCursor": f"c{i}"}}}
        return gql, calls

    def test_pages_to_the_end_past_thirty(self):
        chunks = [[pr(n) for n in range(k * 20, k * 20 + 20)] for k in range(3)]
        gql, calls = self.pages(chunks, 60)
        nodes, complete, total = op.search_prs("o/r", "owner", gql=gql)
        self.assertEqual((len(nodes), complete, total), (60, True, 60))
        self.assertEqual([c["after"] for c in calls], [None, "c0", "c1"])
        self.assertIn("draft:false author:owner", calls[0]["q"])

    def test_a_short_listing_is_incomplete(self):
        gql, _ = self.pages([[pr(1)]], 5)
        self.assertFalse(op.search_prs("o/r", "owner", gql=gql)[1])

    def test_a_timeout_retries_with_a_smaller_page(self):
        sizes = []

        def gql(_q, v):
            sizes.append(v["n"])
            if v["n"] > 5:
                raise RuntimeError("HTTP 504: Gateway Timeout")
            return {"search": {"issueCount": 1, "nodes": [pr(1)], "pageInfo": {"hasNextPage": False}}}
        nodes, complete, _ = op.search_prs("o/r", "owner", gql=gql)
        self.assertEqual((sizes, len(nodes), complete), ([20, 10, 5], 1, True))

    def test_a_page_that_never_loads_is_incomplete_not_empty(self):
        def gql(_q, _v):
            raise subprocess.TimeoutExpired("gh", 90)
        self.assertEqual(op.search_prs("o/r", "owner", gql=gql), ([], False, -1))

    def test_a_low_quota_stops_the_listing(self):
        def gql(_q, _v):
            return {"rateLimit": {"remaining": 40, "resetAt": "T"},
                    "search": {"issueCount": 50, "nodes": [pr(1)], "pageInfo": {"hasNextPage": True, "endCursor": "c"}}}
        with self.assertRaises(op.QuotaLow) as cm:
            op.search_prs("o/r", "owner", gql=gql, min_quota=100)
        self.assertEqual(cm.exception.args[:2], (40, "T"))
        self.assertEqual(len(cm.exception.args[2]), 1)
        self.assertEqual(op.search_prs("o/r", "owner", gql=lambda q, v: {
            "rateLimit": {"remaining": 40}, "search": {"issueCount": 1, "nodes": [pr(1)],
                                                       "pageInfo": {"hasNextPage": False}}}, min_quota=10)[1], True)


class Record(unittest.TestCase):
    def test_acted_holds_until_the_commit_moves(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state" / "own-pr-shepherd.json"
            op.record(path, "o/r#1", "needs_reviewer", HEAD, now=1)
            rec = op.load_record(path)
            self.assertTrue(op.acted(rec, "o/r#1", "needs_reviewer", HEAD))
            self.assertFalse(op.acted(rec, "o/r#1", "ci_red", HEAD))
            self.assertFalse(op.acted(rec, "o/r#1", "needs_reviewer", "new"))
            self.assertFalse(op.acted(rec, "o/r#1", "needs_reviewer", ""))
            op.record(path, "o/r#1", "ci_red", "new", now=2)
            self.assertEqual(json.loads(path.read_text())["o/r#1"],
                             {"acted": {"needs_reviewer": HEAD, "ci_red": "new"}, "at": 2})
            path.write_text(json.dumps({"o/r#1": {"head": HEAD, "acted": ["ci_red"]}}))
            self.assertFalse(op.acted(op.load_record(path), "o/r#1", "ci_red", HEAD), "an old-shape entry")
            op.record(path, "o/r#1", "ci_red", "x", now=3)
            self.assertEqual(op.load_record(path)["o/r#1"], {"acted": {"ci_red": "x"}, "at": 3})

    def test_the_mark_is_the_last_non_merge_commit_except_for_base_classes(self):
        p = merged_base()
        self.assertEqual(op.content_commit(p)["oid"], "c1")
        for cls in ("needs_reviewer", "changes_addressed", "approved"):
            self.assertEqual(op.acted_sha(p, cls), "c1", cls)
        for cls in ("conflicting", "behind", "ci_red"):
            self.assertEqual(op.acted_sha(p, cls), HEAD, "the base moving again needs another merge")
        only_merges = pr(recent=[commit(HEAD, merge=True)])
        self.assertEqual(op.content_commit(only_merges)["oid"], HEAD, "falls back to the head")
        self.assertEqual(op.acted_sha(pr(recent=[]), "approved"), HEAD)

    def test_the_record_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            for i in range(op.RECORD_CAP + 3):
                op.record(path, f"o/r#{i}", "ci_red", HEAD, now=i)
            data = op.load_record(path)
            self.assertEqual(len(data), op.RECORD_CAP)
            self.assertNotIn("o/r#0", data)

    def test_concurrent_records_keep_every_entry(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            real = op.load_record

            def slow(p):
                data = real(p)
                threading.Event().wait(0.01)  # widen the read-modify-write window
                return data
            with mock.patch.object(op, "load_record", slow):
                threads = [threading.Thread(target=op.record, args=(path, f"o/r#{i}", "ci_red", HEAD))
                           for i in range(12)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            self.assertEqual(len(real(path)), 12)

    def test_the_shared_ask_ledger_covers_ask_classes_since_the_content(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "review-asks.jsonl"
            ledger.write_text("\n".join([
                json.dumps({"repo": "o/r", "pr": 1, "reviewer": "r", "ts": "2026-11-14T09:00:00Z"}),
                json.dumps({"repo": "o/r", "pr": 1, "reviewer": "r", "ts": "2026-11-14T11:00:00Z"}),
                "not json", json.dumps({"repo": "o/r"})]) + "\n")
            asks = op.load_asks(ledger)
            self.assertEqual(asks, {"o/r#1": "2026-11-14T11:00:00Z"})
            p = pr(committed="2026-11-14T10:00:00Z")
            self.assertTrue(op.asked_since_content(asks, "o/r#1", "needs_reviewer", p))
            self.assertTrue(op.asked_since_content(asks, "o/r#1", "changes_addressed", p))
            self.assertFalse(op.asked_since_content(asks, "o/r#1", "ci_red", p), "only reviewer asks")
            self.assertFalse(op.asked_since_content(asks, "o/r#1", "needs_reviewer", pr(committed="2026-11-14T12:00:00Z")))
            self.assertFalse(op.asked_since_content(asks, "o/r#2", "needs_reviewer", p))
            self.assertEqual(op.load_asks(Path(d) / "missing"), {})
            self.assertEqual(op.load_asks(None), {})

    def test_an_ask_before_a_base_merge_still_covers_it(self):
        asks = {"o/r#1": "2026-11-14T11:00:00Z"}
        self.assertTrue(op.asked_since_content(asks, "o/r#1", "needs_reviewer", merged_base()),
                        "the merge at 13:00 is newer than the ask; the owner's commit is not")

    def test_the_ledger_path_comes_from_its_writer(self):
        path = op._ledger_path()
        self.assertIsNotNone(path)
        self.assertEqual(path.name, "review-asks.jsonl")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(op, "REPO_ROOT", Path(d)):
            self.assertIsNone(op._ledger_path(), "no collaboration-intelligence skill")

    def test_the_state_lives_under_the_workspace(self):
        path = op.state_path()
        self.assertEqual((path.name, path.parent.name), ("own-pr-shepherd.json", "state"))

    def test_record_refuses_an_unknown_class(self):
        self.assertEqual(op.main(["record", "o/r#1", "bogus", "--sha", HEAD]), 2)

    def test_record_cli_writes_the_mark(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            out = io.StringIO()
            with mock.patch.object(op, "state_path", lambda: path), contextlib.redirect_stdout(out):
                self.assertEqual(op.main(["record", "o/r#1", "approved", "--sha", "c1" * 20]), 0)
            self.assertEqual(out.getvalue().strip(), "RECORDED o/r#1 approved sha=c1c1c1c1")
            self.assertTrue(op.acted(op.load_record(path), "o/r#1", "approved", "c1" * 20))


class GraphQL(unittest.TestCase):
    def test_variables_and_errors(self):
        seen = []

        def runner(cmd, **_kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"data": {"ok": 1}}))
        self.assertEqual(op.gh_graphql("Q", {"q": "s", "n": 5, "after": None}, runner=runner), {"ok": 1})
        self.assertEqual(seen[0], ["gh", "api", "graphql", "-f", "query=Q", "-f", "q=s", "-F", "n=5"])

        def failing(cmd, **_kw):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 504")
        with self.assertRaisesRegex(RuntimeError, "HTTP 504"):
            op.gh_graphql("Q", {}, runner=failing)
        with self.assertRaisesRegex(RuntimeError, "gh api graphql failed"):
            op.gh_graphql("Q", {}, runner=lambda c, **k: subprocess.CompletedProcess(c, 1, stdout="", stderr=""))

        def errors(cmd, **_kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"errors": [{"message": "bad"}]}))
        with self.assertRaisesRegex(RuntimeError, "bad"):
            op.gh_graphql("Q", {}, runner=errors)


class Config(unittest.TestCase):
    def test_repos_have_no_default(self):
        with mock.patch.dict(os.environ, {"OWN_PR_SHEPHERD_REPOS": ""}), \
                mock.patch.object(op.subprocess, "run", side_effect=AssertionError("no gh repo view fallback")):
            self.assertEqual(op.configured_repos([]), [])
        with mock.patch.dict(os.environ, {"OWN_PR_SHEPHERD_REPOS": "a/b, c/d bogus"}):
            self.assertEqual(op.configured_repos([]), ["a/b", "c/d"])
        self.assertEqual(op.configured_repos(["x/y"]), ["x/y"])

    def test_email_from_config_else_git(self):
        def git(_cmd, **_kw):
            return subprocess.CompletedProcess(_cmd, 0, stdout="Me@X.org\n")
        with mock.patch.dict(os.environ, {"OWN_PR_SHEPHERD_EMAIL": ""}):
            self.assertEqual(op.configured_email(None, runner=git), "me@x.org")
            self.assertEqual(op.configured_email(None, runner=lambda c, **k: subprocess.CompletedProcess(c, 1, stdout="")), "")
            self.assertEqual(op.configured_email("Set@Y.org", runner=git), "set@y.org")

            def no_git(_cmd, **_kw):
                raise OSError("git not found")
            self.assertEqual(op.configured_email(None, runner=no_git), "")

    def test_author_from_config_else_viewer(self):
        with mock.patch.dict(os.environ, {"OWN_PR_SHEPHERD_AUTHOR": ""}):
            self.assertEqual(op.configured_author(None, gql=lambda q, v: {"viewer": {"login": "me"}}), "me")
            self.assertEqual(op.configured_author("cli", gql=None), "cli")

    def test_min_quota(self):
        with mock.patch.dict(os.environ, {"OWN_PR_SHEPHERD_MIN_QUOTA": "abc"}):
            self.assertEqual(op.configured_min_quota(), op.DEFAULT_MIN_QUOTA)
        with mock.patch.dict(os.environ, {"OWN_PR_SHEPHERD_MIN_QUOTA": "7"}):
            self.assertEqual(op.configured_min_quota(), 7)


class ListCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.env = mock.patch.dict(os.environ, {"OWN_PR_SHEPHERD_REPOS": "o/r", "OWN_PR_SHEPHERD_EMAIL": OWNER,
                                                "OWN_PR_SHEPHERD_AUTHOR": "owner",
                                                "OWN_PR_SHEPHERD_MIN_QUOTA": "10"})
        self.env.start()
        self.patches = [mock.patch.object(op, "state_path", lambda: self.state),
                        mock.patch.object(op, "_ledger_path", lambda: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def run_list(self, gql, *argv):
        out = io.StringIO()
        args = op.argparse.Namespace(repo=[], author=None, email=None, json=False)
        for k, v in zip(argv[::2], argv[1::2]):
            setattr(args, k, v)
        with contextlib.redirect_stdout(out):
            rc = op.cmd_list(args, gql=gql)
        return rc, out.getvalue()

    @staticmethod
    def search(nodes, total=None, remaining=5000):
        return lambda q, v: {"rateLimit": {"remaining": remaining, "resetAt": "T"},
                             "search": {"issueCount": len(nodes) if total is None else total, "nodes": nodes,
                                        "pageInfo": {"hasNextPage": False}}}

    def test_skips_without_repos_or_email(self):
        with mock.patch.dict(os.environ, {"OWN_PR_SHEPHERD_REPOS": ""}):
            rc, out = self.run_list(None)
        self.assertEqual((rc, out.split()[0]), (0, "SKIP"))
        self.assertIn("OWN_PR_SHEPHERD_REPOS", out)
        with mock.patch.dict(os.environ, {"OWN_PR_SHEPHERD_EMAIL": ""}), \
                mock.patch.object(op, "configured_email", lambda _c: ""):
            rc, out = self.run_list(None)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("SKIP no owner email"))

    def test_skips_when_gh_is_not_signed_in(self):
        def gql(_q, _v):
            raise RuntimeError("gh auth login")
        with mock.patch.dict(os.environ, {"OWN_PR_SHEPHERD_AUTHOR": ""}):
            rc, out = self.run_list(gql)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("SKIP gh is not signed in"))

    def test_lists_only_the_owners_prs(self):
        nodes = [pr(1), pr(2, email="other-agent@x"), pr(3, decision="APPROVED", mss="CLEAN")]
        rc, out = self.run_list(self.search(nodes))
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertTrue(any(l.startswith("PR o/r#1 class=needs_reviewer next=request-review acted=no") for l in lines))
        self.assertFalse(any("o/r#2" in l for l in lines))
        self.assertTrue(any(l.startswith("PR o/r#3 class=approved next=ask-owner") for l in lines))
        self.assertIn("prs=2 not_mine=1 due=2 complete=yes", lines[-1])

    def test_acted_is_keyed_on_the_primary_class_and_commit(self):
        op.record(self.state, "o/r#1", "needs_reviewer", HEAD)
        _rc, out = self.run_list(self.search([pr(1)]))
        self.assertIn("acted=yes", out)
        self.assertIn("due=0", out)
        op.record(self.state, "o/r#1", "ci_red", "other-commit")
        _rc, out = self.run_list(self.search([pr(1)]))
        self.assertIn("acted=yes", out, "another class's mark leaves this one")
        op.record(self.state, "o/r#1", "needs_reviewer", "other-commit")
        _rc, out = self.run_list(self.search([pr(1)]))
        self.assertIn("acted=no", out)

    def test_a_base_merge_after_an_ask_re_pings_no_one(self):
        ledger = Path(self.tmp.name) / "review-asks.jsonl"
        # The reviewer was asked (ledger, 11:00) and the owner was asked (record) about commit c1.
        ledger.write_text(json.dumps({"repo": "o/r", "pr": 1, "ts": "2026-11-14T11:00:00Z"}) + "\n")
        op.record(self.state, "o/r#2", "approved", "c1")
        merged = [merged_base(1), merged_base(2, decision="APPROVED", mss="CLEAN")]
        with mock.patch.object(op, "_ledger_path", lambda: ledger):
            _rc, out = self.run_list(self.search(merged))
        lines = {l.split()[1]: l for l in out.splitlines() if l.startswith("PR ")}
        self.assertIn("class=needs_reviewer next=request-review acted=yes", lines["o/r#1"])
        self.assertIn("class=approved next=ask-owner acted=yes", lines["o/r#2"])
        self.assertIn("sha=c1 ", lines["o/r#2"])
        self.assertIn("due=0", out.splitlines()[-1])

        # A real commit after the merge is new work: both are asked again.
        recent = [commit("c1", "2026-11-14T10:00:00Z"), commit("m", "2026-11-14T13:00:00Z", merge=True),
                  commit(HEAD, "2026-11-14T14:00:00Z")]
        moved = [pr(1, committed="2026-11-14T14:00:00Z", recent=recent),
                 pr(2, decision="APPROVED", mss="CLEAN", committed="2026-11-14T14:00:00Z", recent=recent)]
        with mock.patch.object(op, "_ledger_path", lambda: ledger):
            _rc, out = self.run_list(self.search(moved))
        lines = {l.split()[1]: l for l in out.splitlines() if l.startswith("PR ")}
        self.assertIn("acted=no", lines["o/r#1"])
        self.assertIn("acted=no", lines["o/r#2"])
        self.assertIn("due=2", out.splitlines()[-1])

    def test_a_merge_base_class_is_keyed_on_the_head(self):
        op.record(self.state, "o/r#1", "behind", "c1")
        _rc, out = self.run_list(self.search([merged_base(1, requests=1, mss="BEHIND")]))
        self.assertIn("class=behind next=merge-base acted=no", out, "behind again after the base moved")
        op.record(self.state, "o/r#1", "behind", HEAD)
        _rc, out = self.run_list(self.search([merged_base(1, requests=1, mss="BEHIND")]))
        self.assertIn("acted=yes", out)

    def test_ci_red_after_a_base_merge_is_due_again(self):
        red = {"__typename": "CheckRun", "name": "lint", "conclusion": "FAILURE"}
        # The failure was triaged on the owner's commit c1; a base merge then brings in a new one.
        op.record(self.state, "o/r#1", "ci_red", "c1")
        _rc, out = self.run_list(self.search([merged_base(1, requests=1, rollup="FAILURE", contexts=[red])]))
        self.assertIn("class=ci_red next=triage-ci acted=no", out, "a base merge can bring a new failure")
        self.assertIn("due=1", out.splitlines()[-1])
        op.record(self.state, "o/r#1", "ci_red", HEAD)
        _rc, out = self.run_list(self.search([merged_base(1, requests=1, rollup="FAILURE", contexts=[red])]))
        self.assertIn("acted=yes", out, "triaged once per head")

    def test_the_shared_ledger_marks_an_ask_as_acted(self):
        ledger = Path(self.tmp.name) / "review-asks.jsonl"
        ledger.write_text(json.dumps({"repo": "o/r", "pr": 1, "ts": "2026-11-14T12:00:00Z"}) + "\n")
        with mock.patch.object(op, "_ledger_path", lambda: ledger):
            _rc, out = self.run_list(self.search([pr(1)]))
        self.assertIn("acted=yes", out)

    def test_an_incomplete_listing_exits_1(self):
        rc, out = self.run_list(self.search([pr(1)], total=3))
        self.assertEqual(rc, 1)
        self.assertIn("INCOMPLETE o/r fetched=1 reported=3", out)
        self.assertIn("complete=no", out.splitlines()[-1])

    def test_a_low_quota_ends_the_listing_incomplete(self):
        with mock.patch.dict(os.environ, {"OWN_PR_SHEPHERD_REPOS": "o/r o/s"}):
            calls = []

            def gql(q, v):
                calls.append(v["q"])
                return self.search([pr(1)], remaining=3)(q, v)
            rc, out = self.run_list(gql)
        self.assertEqual(rc, 1)
        self.assertIn("QUOTA_LOW remaining=3 floor=10", out)
        self.assertEqual(len(calls), 1, "the second repo is not listed")

    def test_json_rows(self):
        _rc, out = self.run_list(self.search([pr(1)]), "json", True)
        row = json.loads(out.splitlines()[0])
        self.assertEqual((row["key"], row["next"], row["acted"], row["sha"]),
                         ("o/r#1", "request-review", False, HEAD))
        self.assertNotIn("merge_queue", row)


class Template(unittest.TestCase):
    def test_the_cron_template_runs_this_skill_only_on_opt_in(self):
        crons = json.loads((REPO / "skills" / "schedule-crons" / "crons.example.json").read_text())
        entry = next(e for e in crons if e["name"] == "own-pr-shepherd")
        self.assertEqual(entry.get("prompt_skill"), "own-pr-shepherd")
        self.assertIs(entry.get("opt_in"), True, "never in a new host's starter set")
        self.assertTrue((REPO / "skills" / entry["prompt_skill"] / "SKILL.md").is_file())

    def test_the_seeder_leaves_it_out(self):
        spec = importlib.util.spec_from_file_location(
            "seed_crons", str(REPO / "skills" / "schedule-crons" / "scripts" / "seed_crons.py"))
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
        names = {e.get("name") for e in seed.starter_entries(
            json.loads((REPO / "skills" / "schedule-crons" / "crons.example.json").read_text()))}
        self.assertIn("main-loop", names)
        self.assertNotIn("own-pr-shepherd", names)

    def test_no_always_on_rule_in_core_instructions(self):
        for rel in ("CLAUDE.md", "AGENTS.md"):
            self.assertNotIn("own-pr-shepherd", (REPO / rel).read_text(), rel)


if __name__ == "__main__":
    unittest.main()
