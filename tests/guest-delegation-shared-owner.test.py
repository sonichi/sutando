#!/usr/bin/env python3
"""The non-owner delegation rule has ONE owner, and no adapter carries a copy.

Discord learned that codex must be spawned with `< /dev/null` (without it codex
waits on stdin and can hang to a timeout having produced nothing) and that its
exit code is not evidence of an answer. AG2 Space and Slack did not, because each
carried its own copy of the instruction. That drift is the defect these tests pin.

`src/policy/guardrail.py` owns the text; adapters pass only what genuinely differs
(surface, tier label, result path, per-tier scope).
"""
import importlib.util
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from policy.guardrail import (  # noqa: E402
    SANDBOXED_DELEGATION_CODEX, sandboxed_delegation_lines,
)

ADAPTERS = {
    "ag2space": REPO / "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py",
    "slack": REPO / "src/slack-bridge.py",
}


class TestOwnerContract(unittest.TestCase):
    def test_invocation_is_stdin_safe(self):
        self.assertIn("< /dev/null", SANDBOXED_DELEGATION_CODEX)
        self.assertIn("--sandbox read-only", SANDBOXED_DELEGATION_CODEX)

    def test_exit_code_is_not_evidence(self):
        """The quieter half: a hang announces itself, an exit-0 refusal does not."""
        self.assertIn("exits 0", SANDBOXED_DELEGATION_CODEX)
        self.assertIn("non-empty", SANDBOXED_DELEGATION_CODEX)

    def test_block_is_fenced_and_carries_its_parameters(self):
        lines = sandboxed_delegation_lines(
            "AG2 Space", "GUEST tier", "results/task-X.txt", "SCOPE-SENTINEL")
        body = "\n".join(lines)
        self.assertIn("===SUTANDO SYSTEM INSTRUCTIONS", body)
        self.assertIn("===END SUTANDO SYSTEM INSTRUCTIONS===", body)
        self.assertIn("This AG2 Space task is GUEST tier", body)
        self.assertIn("results/task-X.txt", body)
        self.assertIn("SCOPE-SENTINEL", body)
        self.assertIn("< /dev/null", body)

    def test_no_network_is_stated_and_declining_is_instructed(self):
        """A delegate that cannot fetch the artifact writes a plausible summary
        anyway — the same shape as exit-0-on-refusal, which this block already
        covers. Raised on #3512 by two reviewers independently."""
        self.assertIn("NO NETWORK", SANDBOXED_DELEGATION_CODEX)
        self.assertIn("decline", SANDBOXED_DELEGATION_CODEX)
        lines = sandboxed_delegation_lines(
            "AG2 Space", "GUEST tier", "results/task-X.txt", "SCOPE-SENTINEL")
        self.assertIn("NO NETWORK", "\n".join(lines))

    def test_unavailable_sandbox_has_no_fallback(self):
        """`assert the OUTPUT is non-empty` had no next step, so the branch where
        codex cannot answer was unstated everywhere but Discord — whose bridge
        carries a two-sentinel Stage-2 contract the shared owner never learned."""
        self.assertIn("NO permitted fallback", SANDBOXED_DELEGATION_CODEX)
        self.assertIn("unrestricted core", SANDBOXED_DELEGATION_CODEX)
        for symptom in ("absent", "exiting non-zero", "exiting 0 having written nothing"):
            self.assertIn(symptom, SANDBOXED_DELEGATION_CODEX)
        lines = sandboxed_delegation_lines(
            "AG2 Space", "GUEST tier", "results/task-X.txt", "SCOPE-SENTINEL")
        self.assertIn("NO permitted fallback", "\n".join(lines))

    def test_silence_is_not_an_outcome(self):
        """A guest cannot distinguish a refusal from a dropped task, so the
        unavailable branch must produce a reply rather than nothing."""
        self.assertIn("do not silently skip", SANDBOXED_DELEGATION_CODEX)
        self.assertIn("no inspection was performed", SANDBOXED_DELEGATION_CODEX)
        lines = sandboxed_delegation_lines(
            "AG2 Space", "GUEST tier", "results/task-X.txt", "SCOPE-SENTINEL")
        self.assertIn("no inspection was performed", "\n".join(lines))

    def test_scope_is_a_parameter_not_baked_in(self):
        """Slack's per-tier limits must not leak into the shared text."""
        self.assertNotIn("information-only", SANDBOXED_DELEGATION_CODEX)
        self.assertNotIn("Research, inspect", SANDBOXED_DELEGATION_CODEX)


class TestAdaptersDelegate(unittest.TestCase):
    def test_no_adapter_carries_its_own_invocation(self):
        """The anti-drift pin: a copy is how one surface learned and two didn't."""
        for name, path in ADAPTERS.items():
            with self.subTest(adapter=name):
                src = path.read_text()
                self.assertNotIn("codex exec --sandbox read-only", src)

    def test_each_adapter_binds_the_shared_owner(self):
        for name, path in ADAPTERS.items():
            with self.subTest(adapter=name):
                src = path.read_text()
                self.assertIn("sandboxed_delegation_lines", src)

    def test_ag2space_guest_branch_renders_the_block(self):
        """Exercised end-to-end by src/remote-gateway-bridge.test.py; here we pin
        that the guest branch is the caller, so a refactor cannot silently drop it."""
        src = ADAPTERS["ag2space"].read_text()
        m = re.search(r'sender_tier == "guest":\s*\n(.{0,400})', src, re.S)
        self.assertIsNotNone(m, "guest branch not found")
        self.assertIn("sandboxed_delegation_lines", m.group(1))

    def test_slack_non_owner_branch_renders_the_block(self):
        src = ADAPTERS["slack"].read_text()
        m = re.search(r'access_tier != "owner":\s*\n(.{0,600})', src, re.S)
        self.assertIsNotNone(m, "non-owner branch not found")
        self.assertIn("sandboxed_delegation_lines", m.group(1))


if __name__ == "__main__":
    unittest.main()
