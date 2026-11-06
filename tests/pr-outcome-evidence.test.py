#!/usr/bin/env python3
"""Hermetic acceptance regressions for the reliability experiment. No live tasks.
Run: python3 tests/reliability-v2.test.py
"""
import contextlib
import io
import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
os.environ["SUTANDO_TELEMETRY"] = "0"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evidence = load("github_evidence_acceptance", "skills/review-preflight/scripts/github-evidence.py")
guard = load("github_evidence_guard_acceptance", "hooks/github-evidence-guard.py")


def pr(head="abc", merged=False):
    return {"head": {"sha": head}, "state": "closed" if merged else "open",
            "merged": merged, "merged_at": "2026-10-05T03:00:00Z" if merged else None}


def check(bucket="pass", name="tests"):
    return {"name": name, "state": "SUCCESS" if bucket == "pass" else "FAILURE",
            "bucket": bucket, "link": "https://github.com/o/r/actions/runs/123/job/456"}


def receipt(before=None, checks=None, after=None, code=0, raw=None, expected=None):
    answers = iter([SimpleNamespace(returncode=0, stdout=json.dumps(before or pr())),
                    SimpleNamespace(returncode=code, stdout=raw if raw is not None else json.dumps(checks)),
                    SimpleNamespace(returncode=0, stdout=json.dumps(after or pr()))])
    return evidence.collect("o/r", 1, expected, runner=lambda args: next(answers))


class AccuracyAcceptance(unittest.TestCase):
    def test_open_green_pr_is_never_reported_merged(self):
        got = receipt(checks=[check()])
        self.assertEqual(got["merge_readiness"], "checks_passed")
        self.assertFalse(got["merged"])

    def test_real_merge_timestamp_is_preserved(self):
        got = receipt(before=pr(merged=True), after=pr(merged=True), checks=[check()])
        self.assertTrue(got["merged"])

    def test_failed_pending_cancelled_skipped_required_check_blocks(self):
        for bucket, code in (("fail", 1), ("pending", 8), ("cancel", 1), ("skipping", 0)):
            with self.subTest(bucket=bucket):
                self.assertEqual(receipt(checks=[check(bucket)], code=code)["merge_readiness"], "blocked")

    def test_missing_empty_malformed_error_are_not_green(self):
        for checks, code, raw in ((None, 0, None), ([], 0, None), ({}, 0, None),
                                  ([{"name": "tests"}], 0, None), (None, 1, "not JSON"),
                                  ([check()], 127, None)):
            with self.subTest(checks=checks, code=code, raw=raw):
                got = receipt(checks=checks, code=code, raw=raw)
                self.assertEqual(got["merge_readiness"], "unknown")
                self.assertTrue(got["errors"])

    def test_head_changes_and_stale_expected_head_are_unknown(self):
        self.assertEqual(receipt(after=pr("def"), checks=[check()])["evidence_status"], "unknown")
        self.assertEqual(receipt(checks=[check()], expected="def")["evidence_status"], "unknown")

    def test_repeated_check_name_cannot_hide_failure(self):
        got = receipt(checks=[check(), check("fail")], code=1)
        self.assertEqual(got["merge_readiness"], "unknown")
        self.assertTrue(got["errors"])

    def test_network_failure_is_unknown(self):
        def fail(args):
            raise subprocess.TimeoutExpired(args, 25)
        got = evidence.collect("o/r", 1, runner=fail)
        self.assertEqual(got["evidence_status"], "unknown")

    def test_rejects_invalid_repository(self):
        with self.assertRaises(ValueError):
            evidence.collect("o/r; echo x", 1)


class EffectiveHookAcceptance(unittest.TestCase):
    def test_hook_entrypoint_and_unavailable_context_remain_unknown(self):
        event = {"tool_name": "Bash", "hook_event_name": "PostToolUse",
                 "tool_input": {"command": "gh pr view 1 --repo o/r"}}
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
                patch.object(guard, "decision_context", side_effect=OSError("unavailable")), \
                contextlib.redirect_stdout(output):
            guard.main()
        self.assertIn("unknown", json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"])
        for data in ({"tool_name": "Read"}, {"tool_name": "Bash", "tool_input": {"command": 3}},
                     {**event, "hook_event_name": "Other"}):
            self.assertEqual(guard.decide(data), {})

    def test_hook_target_parser_preserves_read_only_scope(self):
        self.assertEqual(guard.decision_targets("gh pr view https://github.com/o/r/pull/1"), [("o/r", 1)])
        self.assertEqual(guard.decision_targets("gh api repos/o/r/pulls/1"), [("o/r", 1)])
        for command in ("gh api repos/o/r/pulls/1 -X POST", "gh pr view 1 --repo", "echo nothing"):
            self.assertEqual(guard.decision_targets(command), [])
        with patch.object(Path, "is_file", return_value=False):
            self.assertIn("unknown", guard.decision_context("gh pr view 1 --repo o/r"))

    def test_known_lossy_ci_query_is_denied(self):
        for filter_command in ("grep FAIL | head -8", "rg error", "head -8", "tail -8", "sed -n '1,8p'", "awk '{print $1}'"):
            got = guard.decide({"tool_name": "Bash", "tool_input": {
                "command": "gh run view 123 --log-failed | " + filter_command}})
            self.assertEqual(got["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_structured_read_and_unfiltered_logs_are_allowed(self):
        for command in ("gh run view 123 --json jobs,headSha,attempt", "gh run view 123 --job 456 --log-failed", "rg FAIL my-local-file", "echo gh status"):
            self.assertEqual(guard.decide({"tool_name": "Bash", "tool_input": {"command": command}}), {})

    def test_post_tool_surfaces_receipt_path_and_unknown_rule(self):
        with patch.object(guard, "decision_context", return_value="Decision evidence fixture"):
            got = guard.decide({"tool_name": "Bash", "hook_event_name": "PostToolUse", "tool_input": {"command": "gh pr view 1 --repo o/r"}})
        self.assertIn("github-evidence.py", got["hookSpecificOutput"]["additionalContext"])
        self.assertIn("unknown, not passed", got["hookSpecificOutput"]["additionalContext"])





if __name__ == "__main__":
    unittest.main(verbosity=2)
