#!/usr/bin/env python3
"""Adversarial status publication cases; no GitHub or messaging I/O."""
import copy
import datetime
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/review-preflight/scripts"))
from outcome_claims import summarize as classify, render
from decision_evidence import fingerprint

NOW = datetime.datetime.fromisoformat("2026-10-05T06:10:00+00:00").timestamp()
STATUS_PATH = Path(__file__).resolve().parents[1] / "skills/review-preflight/scripts/github-status.py"
spec = importlib.util.spec_from_file_location("status_test", STATUS_PATH)
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


def summarize(checks, decision):
    result = classify(checks, decision, NOW)
    result["observed_at"] = "2026-10-05T06:10:00Z"
    return result


def receipts():
    base = {"repository": "o/r", "pr": 1, "head_sha": "abc", "evidence_status": "stable",
            "errors": [], "state": "open", "merged": False, "merged_at": None,
            "observed_at": "2026-10-05T06:09:59Z"}
    checks = dict(base, merge_readiness="checks_passed", required_checks=[{"name": "tests", "bucket": "pass", "link": "https://example.test/check"}])
    decision = dict(base, review_decision="APPROVED", owner_decision="resolved")
    return checks, decision


class OutcomeTests(unittest.TestCase):
    def test_open_green_approved_still_cannot_claim_overall_ready(self):
        result = summarize(*receipts())
        self.assertEqual(result["checks_status"], "passed")
        self.assertEqual(result["merge_outcome"], "not_merged")
        self.assertEqual(result["overall_readiness"], "unknown")
        self.assertIn("Overall readiness: unknown", render(result))

    def test_model_supplied_authority_and_readiness_are_ignored(self):
        checks, decision = receipts()
        for receipt in (checks, decision):
            receipt.update(overall_readiness="ready", owner_authority=True, human_approved=True)
        result = summarize(checks, decision)
        self.assertEqual(result["owner_authority"], "unknown")
        self.assertEqual(result["overall_readiness"], "unknown")
        self.assertIn("Overall readiness: unknown", render(dict(result, overall_readiness="ready")))

    def test_missing_error_stale_and_cross_repo_receipts_stay_unknown(self):
        for field, value in (("repository", "x/y"), ("pr", 2), ("head_sha", "def"), ("evidence_status", "unknown"), ("errors", ["timeout"]), ("merged", True), ("state", "closed")):
            with self.subTest(field=field):
                checks, decision = receipts()
                decision[field] = value
                result = summarize(checks, decision)
                self.assertEqual(result["merge_outcome"], "unknown")
                self.assertEqual(result["checks_status"], "unknown")
                self.assertTrue(result["errors"])
        self.assertTrue(summarize(None, {})["errors"])

    def test_duplicate_checks_never_choose_newest_green(self):
        checks, decision = receipts()
        checks["required_checks"].append(copy.deepcopy(checks["required_checks"][0]))
        self.assertEqual(summarize(checks, decision)["checks_status"], "unknown")

    def test_receipts_require_current_timezone_aware_timestamps(self):
        for value in (None, "yesterday", "2026-10-05T06:09:00", "2026-10-05T06:00:00Z", "2026-10-05T06:11:00Z"):
            checks, decision = receipts()
            decision["observed_at"] = value
            self.assertEqual(summarize(checks, decision)["merge_outcome"], "unknown")

    def test_boolean_pr_cannot_alias_integer_identity(self):
        checks, decision = receipts()
        decision["pr"] = True
        self.assertTrue(summarize(checks, decision)["errors"])

    def test_each_nonpassing_required_check_blocks(self):
        for bucket in ("fail", "pending", "skipping", "cancel"):
            checks, decision = receipts()
            checks["required_checks"][0]["bucket"] = bucket
            self.assertEqual(summarize(checks, decision)["overall_readiness"], "blocked")

    def test_review_hold_blocks_even_green(self):
        for state in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
            checks, decision = receipts()
            decision["review_decision"] = state
            self.assertEqual(summarize(checks, decision)["overall_readiness"], "blocked")

    def test_merge_requires_matching_server_timestamp_not_checks(self):
        checks, decision = receipts()
        for r in (checks, decision):
            r.update(merged=True, state="closed", merged_at="2026-10-05T06:00:00Z")
        checks["required_checks"] = []
        result = summarize(checks, decision)
        self.assertEqual(result["merge_outcome"], "merged")
        self.assertEqual(result["checks_status"], "unknown")
        decision["merged_at"] = "different"
        self.assertEqual(summarize(checks, decision)["merge_outcome"], "unknown")


class CollectionTests(unittest.TestCase):
    def collect(self, after=None, runner=None, budget=24):
        checks, policy = receipts()
        for r in (checks, policy):
            r["observed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        pr = {"head": {"sha": "abc"}, "base": {"ref": "main"}, "state": "open", "merged": False, "merged_at": None, "body": "current", "updated_at": "now"}
        policy["metadata_digest"] = fingerprint(pr)
        fake = runner or (lambda args: SimpleNamespace(returncode=0, stdout=json.dumps(pr if after is None else after)))
        with patch.object(status.decision, "collect", return_value=policy), patch.object(status.evidence, "collect", return_value=checks):
            return status.collect("o/r", 1, runner=fake, budget=budget)

    def test_stable_receipts_render_checks_without_overall_ready(self):
        got = self.collect()
        self.assertEqual(got["checks_status"], "passed")
        self.assertEqual(got["overall_readiness"], "unknown")
        self.assertIn("Not merged", got["body"])

    def test_same_head_body_change_suppresses_all_published_claims(self):
        got = self.collect(after={"head": {"sha": "abc"}, "body": "different owner hold"})
        self.assertEqual(got["merge_outcome"], "unknown")
        self.assertEqual(got["checks_status"], "unknown")
        self.assertNotIn("Required checks: passed", got["body"])

    def test_final_timeout_and_budget_exhaustion_are_unknown(self):
        def timeout(args):
            raise subprocess.TimeoutExpired(args, 6)
        self.assertTrue(self.collect(runner=timeout)["errors"])
        self.assertTrue(self.collect(budget=0)["errors"])

    def test_only_read_calls_and_no_input_receipt_replay(self):
        calls = []
        def fail(args):
            calls.append(args)
            return SimpleNamespace(returncode=1, stdout="")
        got = status.collect("o/r", 1, runner=fail)
        self.assertTrue(got["errors"])
        self.assertTrue(calls)
        self.assertTrue(all(a[:2] in (["gh", "api"], ["gh", "pr"]) for a in calls))
        self.assertFalse(any("--method" in a or "review" in a or "merge" in a for a in calls))


class PublicationTests(unittest.TestCase):
    def fixture(self, status_value="approved", event=True):
        calls = []
        def runtime(args):
            calls.append(args)
            if args[2:4] == ["approval", "request"]:
                value = {"requestId": "approval-1", "status": "pending"}
            elif args[2:4] == ["request", "wait"]:
                value = {"requestId": "approval-1", "status": status_value}
            else:
                value = {"requestId": "execution-1", "status": "completed", "result": {"executed": True}}
                if event: value["result"]["eventId"] = "$e"
            return SimpleNamespace(returncode=0, stdout=json.dumps(value))
        return calls, runtime

    def test_canonical_body_is_identical_in_approval_and_execution(self):
        result = summarize(*receipts())
        result.update(body="Ready to merge", overall_readiness="ready")
        calls, runtime = self.fixture()
        got = status.publish(result, "o/r", 1, "!r:x", "fixture", runner=runtime, now=NOW)
        self.assertEqual(got["event_id"], "$e")
        self.assertEqual(len(calls), 3)
        for call in (calls[0], calls[2]):
            body = json.loads(call[call.index("--input")+1])["body"]
            self.assertNotIn("Ready to merge", body)
            self.assertIn("Overall readiness: unknown", body)
        self.assertEqual(calls[0][4:10], calls[2][4:10])
        self.assertIn("--approval", calls[2])
        self.assertIn("--idempotency-key", calls[2])

    def test_unapproved_or_expired_never_executes(self):
        for resolution in ("pending", "denied", "cancelled", "expired", "failed"):
            calls, runtime = self.fixture(resolution)
            got = status.publish(summarize(*receipts()), "o/r", 1, "!r:x", "fixture", runner=runtime, now=NOW)
            self.assertFalse(got["ok"])
            self.assertEqual(len(calls), 2)
        calls, runtime = self.fixture()
        clock = iter((NOW, NOW+31))
        got = status.publish(summarize(*receipts()), "o/r", 1, "!r:x", "fixture", runner=runtime, now=lambda: next(clock))
        self.assertEqual(got["state"], "EXPIRED")
        self.assertEqual(len(calls), 2)

    def test_unknown_identity_or_stale_observation_never_requests_approval(self):
        def forbidden(args): self.fail("Stale or misaddressed receipt requested approval")
        for repo, number, now in (("other/repo",1,NOW),("o/r",2,NOW),("o/r",1,NOW+31),("o/r",1,NOW-1)):
            self.assertFalse(status.publish(summarize(*receipts()), repo, number, "!r:x", "fixture", runner=forbidden, now=now)["ok"])
        self.assertFalse(status.publish({"errors":["head moved"]}, "o/r", 1, "!r:x", "fixture", runner=forbidden)["ok"])

    def test_execution_timeout_is_unknown_without_retry(self):
        calls, runtime = self.fixture()
        def timeout(args):
            if args[2:4] == ["capability", "execute"]:
                calls.append(args)
                raise subprocess.TimeoutExpired(args,20)
            return runtime(args)
        got = status.publish(summarize(*receipts()), "o/r", 1, "!r:x", "fixture", runner=timeout, now=NOW)
        self.assertEqual(got["state"], "OUTCOME_UNKNOWN")
        self.assertEqual(len(calls), 3)

    def test_unconfirmed_execution_is_unknown(self):
        calls, runtime = self.fixture(event=False)
        got = status.publish(summarize(*receipts()), "o/r", 1, "!r:x", "fixture", runner=runtime, now=NOW)
        self.assertEqual(got["state"], "OUTCOME_UNKNOWN")

    def test_malformed_approval_never_executes(self):
        for text in ("not JSON", "[]", '{"requestId":"a","status":"approved"}'):
            got = status.publish(summarize(*receipts()), "o/r", 1, "!r:x", "fixture", runner=lambda args:SimpleNamespace(returncode=0,stdout=text), now=NOW)
            self.assertEqual(got["state"], "APPROVAL_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
