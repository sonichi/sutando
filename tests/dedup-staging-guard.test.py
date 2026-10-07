#!/usr/bin/env python3
"""PreToolUse dedup-staging-guard: a Bash `mv` landing a dedup-staged result
into `results/`, run via ANY caller, is gated on check-dedup-targets.py
regardless of whether the calling skill remembers to chain step 1's `&&`
itself (hooks/dedup-staging-guard.py).

Run:  python3 tests/dedup-staging-guard.test.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "dedup-staging-guard.py"
_spec = importlib.util.spec_from_file_location("dsg", HOOK)
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)

SAFE_CHAINED = (
    'S="$WORKSPACE/state/dedup-staging/task-foo.txt"; '
    'python3 skills/proactive-loop/scripts/check-dedup-targets.py "$S" && '
    'mv -f "$S" "$WORKSPACE/results/task-foo.txt"'
)
BYPASS_VIA_VAR = (
    'S="$WORKSPACE/state/dedup-staging/task-foo.txt"; '
    'mv -f "$S" "$WORKSPACE/results/task-foo.txt"'
)
BYPASS_LITERAL = (
    'mv -f "$WORKSPACE/state/dedup-staging/task-foo.txt" "$WORKSPACE/results/task-foo.txt"'
)


class MvSegments(unittest.TestCase):
    def test_finds_the_mv_segment_after_the_staging_assignment(self):
        segs = G._mv_segments(BYPASS_VIA_VAR)
        self.assertEqual(len(segs), 1)
        self.assertIn("$S", segs[0])

    def test_an_and_chain_splits_the_checker_call_from_the_mv(self):
        """Ground truth for the design note in the module docstring: `&&`
        splits into a SEPARATE segment, so the checker call and the `mv` it
        gates never share a segment in the canonical step-1 invocation."""
        segs = G._shell_scan.segments(SAFE_CHAINED)
        self.assertEqual(len(segs), 3)
        self.assertTrue(any(w.text.endswith("check-dedup-targets.py") for w in segs[1]))
        self.assertTrue(any(w.basename_is("mv") for w in segs[2]))
        self.assertIsNot(segs[1], segs[2])

    def test_no_mv_in_command_returns_no_segments(self):
        self.assertEqual(G._mv_segments('echo "moving on"'), [])

    def test_a_path_qualified_mv_is_still_mv(self):
        segs = G._mv_segments('/bin/mv -f "$S" "$WORKSPACE/results/x.txt"')
        self.assertEqual(len(segs), 1)

    def test_an_unrelated_mv_in_another_segment_is_not_conflated(self):
        segs = G._mv_segments('mv /tmp/a /tmp/b && mv -f "$S" "$WORKSPACE/results/x.txt"')
        self.assertEqual(len(segs), 2)


class CheckDedupStagingBypass(unittest.TestCase):
    def test_the_safe_chained_command_allows(self):
        self.assertIsNone(G.check_dedup_staging_bypass(SAFE_CHAINED))

    def test_the_variable_bypass_denies(self):
        reason = G.check_dedup_staging_bypass(BYPASS_VIA_VAR)
        self.assertIsNotNone(reason)
        self.assertIn("mv", reason)

    def test_the_fully_literal_bypass_denies(self):
        reason = G.check_dedup_staging_bypass(BYPASS_LITERAL)
        self.assertIsNotNone(reason)

    def test_an_unrelated_mv_is_not_a_match(self):
        self.assertIsNone(G.check_dedup_staging_bypass('mv -f /tmp/a /tmp/b'))

    def test_results_with_no_dedup_staging_mention_fails_open(self):
        self.assertIsNone(G.check_dedup_staging_bypass('mv -f /tmp/a "$WORKSPACE/results/x.txt"'))

    def test_dedup_staging_mention_with_no_results_destination_fails_open(self):
        self.assertIsNone(G.check_dedup_staging_bypass(
            'mv -f "$WORKSPACE/state/dedup-staging/x.txt" /tmp/elsewhere.txt'))

    def test_a_dedup_staging_mention_with_no_mv_at_all_is_not_a_match(self):
        self.assertIsNone(G.check_dedup_staging_bypass(
            'echo "state/dedup-staging/x.txt" && cat "$WORKSPACE/results/x.txt"'))

    def test_the_checker_call_anywhere_in_the_command_allows_even_split_by_semicolon(self):
        cmd = (
            'python3 skills/proactive-loop/scripts/check-dedup-targets.py '
            '"$WORKSPACE/state/dedup-staging/x.txt"; '
            'mv -f "$WORKSPACE/state/dedup-staging/x.txt" "$WORKSPACE/results/x.txt"'
        )
        self.assertIsNone(G.check_dedup_staging_bypass(cmd))

    def test_an_unrelated_mv_in_one_segment_does_not_combine_with_a_dedup_mv_in_another(self):
        """Two separate mv's: one plain, one the real bypass. The DENY must
        still fire (from the second mv), proving detection isn't accidentally
        disabled by the first mv's presence — not a false negative from
        segment confusion."""
        cmd = 'mv /tmp/a /tmp/b && ' + BYPASS_VIA_VAR
        reason = G.check_dedup_staging_bypass(cmd)
        self.assertIsNotNone(reason)

    def test_a_non_string_command_fails_open(self):
        self.assertIsNone(G.check_dedup_staging_bypass(None))


class EndToEnd(unittest.TestCase):
    def test_a_non_bash_tool_is_ignored(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Edit", "tool_input": {
                "file_path": "state/dedup-staging/x.txt"}}),
            capture_output=True, text=True,
        )
        self.assertNotIn('"permissionDecision"', r.stdout)

    def test_a_non_matching_bash_command_is_ignored(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}),
            capture_output=True, text=True,
        )
        self.assertNotIn('"permissionDecision"', r.stdout)

    def test_the_safe_chained_command_is_allowed(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": SAFE_CHAINED}}),
            capture_output=True, text=True,
        )
        self.assertNotIn('"permissionDecision"', r.stdout)

    def test_the_bypass_command_is_denied(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": BYPASS_VIA_VAR}}),
            capture_output=True, text=True,
        )
        self.assertIn('"permissionDecision": "deny"', r.stdout)
        self.assertIn("dedup-staging-guard", r.stdout)

    def test_the_override_env_var_bypasses_everything(self):
        env = dict(os.environ)
        env["SUTANDO_ALLOW_UNGATED_DEDUP_STAGING"] = "1"
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": BYPASS_VIA_VAR}}),
            capture_output=True, text=True, env=env,
        )
        self.assertNotIn('"permissionDecision"', r.stdout)

    def test_malformed_stdin_does_not_crash(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)], input="not json", capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0)
        self.assertNotIn('"permissionDecision"', r.stdout)


if __name__ == "__main__":
    unittest.main()
