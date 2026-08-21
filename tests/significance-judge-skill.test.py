#!/usr/bin/env python3
"""Tests for skills/significance-judge/scripts/judge.py.

The subagent is ALWAYS a stub fixture script injected via SIGNIFICANCE_JUDGE_CMD
— no test invokes a real model. Covers: valid stdin + valid stub output → the
strict stdout contract; malformed subagent output → non-zero + empty stdout;
malformed stdin → non-zero + empty stdout; prompt delivery on the child's stdin.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
JUDGE = REPO / "skills" / "significance-judge" / "scripts" / "judge.py"


def _event(event_id: str, **overrides) -> dict:
    row = {
        "id": event_id,
        "ts": "2026-08-21T00:00:00Z",
        "source": "discord",
        "kind": "message",
        "actor_id": "actor",
        "title": "shipped the release",
        "detail": "merged and tagged",
        "place": "",
        "url": "",
    }
    row.update(overrides)
    return row


def _request(events: list) -> str:
    return json.dumps({
        "schema_version": 1,
        "instructions": "Select only events that genuinely matter.",
        "events": events,
    })


class JudgeHarness(unittest.TestCase):
    """Run judge.py against a stub subagent script emitting canned stdout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def stub_cmd(self, body: str) -> str:
        """Write a stub subagent script; return the SIGNIFICANCE_JUDGE_CMD value."""
        stub = self.tmp / "stub_agent.py"
        stub.write_text(body)
        return f"{sys.executable} {stub}"

    def run_judge(self, stdin: str, stub_body: str | None = None, **env_extra) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("SIGNIFICANCE_JUDGE_CMD", None)
        if stub_body is not None:
            env["SIGNIFICANCE_JUDGE_CMD"] = self.stub_cmd(stub_body)
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(JUDGE)],
            input=stdin, capture_output=True, text=True, env=env, timeout=60,
        )


STUB_VALID = """\
import json, sys
sys.stdin.read()
print(json.dumps([
    {"event_id": "ev-1", "significance_score": 0.9, "reason": "shipped milestone"},
    {"event_id": "ev-2", "significance_score": 0.2, "reason": "routine"},
]))
"""


class TestHappyPath(JudgeHarness):
    def test_valid_stdin_and_stub_produces_contract_stdout(self):
        result = self.run_judge(_request([_event("ev-1"), _event("ev-2")]), STUB_VALID)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(rows, [
            {"event_id": "ev-1", "significance_score": 0.9, "reason": "shipped milestone"},
            {"event_id": "ev-2", "significance_score": 0.2, "reason": "routine"},
        ])

    def test_prompt_reaches_subagent_stdin_with_rubric_and_events(self):
        capture = self.tmp / "prompt.txt"
        stub = (
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(capture)!r}).write_text(sys.stdin.read())\n"
            "print(json.dumps([{'event_id': 'ev-1', 'significance_score': 1, 'reason': 'ok'}]))\n"
        )
        result = self.run_judge(_request([_event("ev-1")]), stub)
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = capture.read_text()
        self.assertIn("shipped milestones", prompt, "rubric missing from prompt")
        self.assertIn("genuinely matter", prompt, "caller instructions missing")
        self.assertIn('"ev-1"', prompt, "event batch missing from prompt")
        self.assertEqual(json.loads(result.stdout)[0]["significance_score"], 1.0)

    def test_code_fenced_subagent_output_is_unwrapped(self):
        stub = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print('```json')\n"
            "print(json.dumps([{'event_id': 'ev-1', 'significance_score': 0.5, 'reason': 'r'}]))\n"
            "print('```')\n"
        )
        result = self.run_judge(_request([_event("ev-1")]), stub)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]["event_id"], "ev-1")


class TestMalformedSubagentOutput(JudgeHarness):
    def assert_rejected(self, stub_body: str, stderr_fragment: str):
        result = self.run_judge(_request([_event("ev-1")]), stub_body)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "", "rejection must leave stdout empty")
        self.assertIn(stderr_fragment, result.stderr)

    def test_non_json_output(self):
        self.assert_rejected(
            "import sys; sys.stdin.read(); print('I judged the events!')",
            "not valid JSON")

    def test_non_array_output(self):
        self.assert_rejected(
            "import sys; sys.stdin.read(); print('{}')",
            "must be a JSON array")

    def test_unknown_event_id(self):
        self.assert_rejected(
            "import json, sys; sys.stdin.read(); "
            "print(json.dumps([{'event_id': 'invented', 'significance_score': 0.5, 'reason': 'r'}]))",
            "unknown event_id")

    def test_duplicate_event_id(self):
        self.assert_rejected(
            "import json, sys; sys.stdin.read(); "
            "print(json.dumps([{'event_id': 'ev-1', 'significance_score': 0.5, 'reason': 'r'}] * 2))",
            "duplicate event_id")

    def test_out_of_range_score(self):
        self.assert_rejected(
            "import json, sys; sys.stdin.read(); "
            "print(json.dumps([{'event_id': 'ev-1', 'significance_score': 1.5, 'reason': 'r'}]))",
            "0 to 1")

    def test_empty_reason(self):
        self.assert_rejected(
            "import json, sys; sys.stdin.read(); "
            "print(json.dumps([{'event_id': 'ev-1', 'significance_score': 0.5, 'reason': '  '}]))",
            "non-empty string")

    def test_extra_key_in_judgment(self):
        self.assert_rejected(
            "import json, sys; sys.stdin.read(); "
            "print(json.dumps([{'event_id': 'ev-1', 'significance_score': 0.5, 'reason': 'r', 'x': 1}]))",
            "each judgment must be")

    def test_subagent_nonzero_exit(self):
        self.assert_rejected(
            "import sys; sys.stdin.read(); print('[]'); sys.exit(3)",
            "exited 3")


class TestMalformedStdin(JudgeHarness):
    def assert_rejected(self, stdin: str, stderr_fragment: str):
        # Stub would succeed — the failure must come from stdin validation,
        # before any subagent is spawned.
        result = self.run_judge(stdin, STUB_VALID)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "", "rejection must leave stdout empty")
        self.assertIn(stderr_fragment, result.stderr)

    def test_non_json_stdin(self):
        self.assert_rejected("not json", "stdin is not valid JSON")

    def test_non_object_stdin(self):
        self.assert_rejected("[]", "must be a JSON object")

    def test_wrong_schema_version(self):
        self.assert_rejected(
            json.dumps({"schema_version": 2, "instructions": "", "events": [_event("ev-1")]}),
            "schema_version")

    def test_events_not_a_list(self):
        self.assert_rejected(
            json.dumps({"schema_version": 1, "instructions": "", "events": {}}),
            "events must be a JSON array")

    def test_event_without_id(self):
        self.assert_rejected(
            json.dumps({"schema_version": 1, "instructions": "", "events": [{"ts": "x"}]}),
            "non-empty string id")

    def test_empty_events(self):
        self.assert_rejected(
            json.dumps({"schema_version": 1, "instructions": "", "events": []}),
            "events is empty")


class TestEnvironment(JudgeHarness):
    def test_missing_agent_cli_fails_before_stdout(self):
        result = self.run_judge(
            _request([_event("ev-1")]),
            stub_body=None,
            SIGNIFICANCE_JUDGE_CMD="definitely-not-a-real-agent-cli-9x7",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("not found on PATH", result.stderr)

    def test_bad_timeout_env_rejected(self):
        result = self.run_judge(
            _request([_event("ev-1")]), STUB_VALID,
            SIGNIFICANCE_JUDGE_TIMEOUT="soon")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("SIGNIFICANCE_JUDGE_TIMEOUT", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
