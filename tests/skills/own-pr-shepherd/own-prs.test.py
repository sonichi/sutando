#!/usr/bin/env python3
"""skills/own-pr-shepherd/scripts/own_prs.py: the classifier over fabricated PR nodes, search
paging to the end (the `--limit` trap) and the incomplete verdict, the smaller-page retry, the
merge-queue gate on `approved`, and the acted-at-head record. No network: GraphQL is a stub."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
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


def pr(number=1, decision="REVIEW_REQUIRED", mergeable="MERGEABLE", mss="BLOCKED", rollup="SUCCESS",
       contexts=(), reviews=(), requests=0, queued=False, draft=False, committed="2026-11-14T10:00:00Z"):
    return {
        "number": number, "url": f"https://x/{number}", "isDraft": draft, "isInMergeQueue": queued,
        "headRefOid": HEAD, "baseRefName": "main", "reviewDecision": decision,
        "mergeable": mergeable, "mergeStateStatus": mss, "author": {"login": "owner"},
        "reviewRequests": {"totalCount": requests},
        "latestReviews": {"nodes": list(reviews)},
        "commits": {"nodes": [{"commit": {"oid": HEAD, "committedDate": committed,
                                          "statusCheckRollup": {"state": rollup,
                                                                "contexts": {"nodes": list(contexts)}}}}]},
    }


def review(state, oid="", at="2026-11-14T11:00:00Z", who="rev"):
    return {"state": state, "submittedAt": at, "author": {"login": who}, "commit": {"oid": oid}}


class Classify(unittest.TestCase):
    def test_changes_requested_on_the_current_head_is_a_fix(self):
        p = pr(decision="CHANGES_REQUESTED", reviews=[review("CHANGES_REQUESTED", oid=HEAD)])
        self.assertEqual(op.classify(p), ["changes_requested"])
        self.assertEqual(op.next_step("changes_requested", None), "fix")

    def test_head_moved_past_the_request_is_a_re_solicit(self):
        p = pr(decision="CHANGES_REQUESTED", reviews=[review("CHANGES_REQUESTED", oid="old")])
        self.assertEqual(op.classify(p), ["changes_addressed"])

    def test_without_a_review_commit_the_dates_decide(self):
        before = review("CHANGES_REQUESTED", at="2026-11-14T09:00:00Z")
        after = review("CHANGES_REQUESTED", at="2026-11-14T11:00:00Z")
        self.assertEqual(op.classify(pr(decision="CHANGES_REQUESTED", reviews=[before])), ["changes_addressed"])
        self.assertEqual(op.classify(pr(decision="CHANGES_REQUESTED", reviews=[after])), ["changes_requested"])

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

    def test_approved_enqueues_only_where_the_base_has_a_merge_queue(self):
        p = pr(decision="APPROVED", mss="CLEAN")
        self.assertEqual(op.classify(p), ["approved"])
        self.assertEqual(op.next_step("approved", True), "enqueue")
        self.assertEqual(op.next_step("approved", False), "ask-owner")
        self.assertEqual(op.next_step("approved", None), "ask-owner")

    def test_approved_with_ci_running_waits(self):
        self.assertEqual(op.classify(pr(decision="APPROVED", rollup="PENDING")), ["approved_ci_pending"])

    def test_already_queued_is_left_alone(self):
        self.assertEqual(op.classify(pr(decision="APPROVED", queued=True)), ["queued"])

    def test_unknown_mergeability_is_rechecked(self):
        self.assertEqual(op.classify(pr(requests=1, mergeable="UNKNOWN", mss="UNKNOWN")), ["unknown"])


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

    def test_merge_queue_read(self):
        def gql(_q, _v, q=None):
            return {"repository": {"mergeQueue": q}}
        self.assertTrue(op.has_merge_queue("o/r", "main", gql=lambda q, v: gql(q, v, {"id": "MQ"})))
        self.assertFalse(op.has_merge_queue("o/r", "main", gql=lambda q, v: gql(q, v)))
        self.assertIsNone(op.has_merge_queue("o/r", "main", gql=lambda q, v: (_ for _ in ()).throw(RuntimeError())))


class Record(unittest.TestCase):
    def test_acted_holds_until_the_head_moves(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state" / "own-pr-shepherd.json"
            op.record(path, "o/r#1", "needs_reviewer", HEAD, now=1)
            rec = op.load_record(path)
            self.assertTrue(op.acted(rec, "o/r#1", "needs_reviewer", HEAD))
            self.assertFalse(op.acted(rec, "o/r#1", "ci_red", HEAD))
            self.assertFalse(op.acted(rec, "o/r#1", "needs_reviewer", "new"))
            op.record(path, "o/r#1", "ci_red", "new", now=2)
            self.assertEqual(json.loads(path.read_text())["o/r#1"], {"head": "new", "acted": ["ci_red"], "at": 2})

    def test_the_record_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            for i in range(op.RECORD_CAP + 3):
                op.record(path, f"o/r#{i}", "ci_red", HEAD, now=i)
            data = op.load_record(path)
            self.assertEqual(len(data), op.RECORD_CAP)
            self.assertNotIn("o/r#0", data)

    def test_record_refuses_an_unknown_class(self):
        self.assertEqual(op.main(["record", "o/r#1", "bogus", "--head", HEAD]), 2)


class Template(unittest.TestCase):
    def test_the_cron_template_runs_this_skill(self):
        crons = json.loads((REPO / "skills" / "schedule-crons" / "crons.example.json").read_text())
        entry = next(e for e in crons if e["name"] == "own-pr-shepherd")
        self.assertEqual(entry.get("prompt_skill"), "own-pr-shepherd")
        self.assertNotIn("opt_in", entry)
        self.assertTrue((REPO / "skills" / entry["prompt_skill"] / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
