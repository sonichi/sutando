#!/usr/bin/env python3
"""The AG2 Space tier rule and its SYSTEM INSTRUCTIONS have one shared owner.

`broker_attested_tier` (src/local_task_protocol.py) and `ag2space_tier_lines`
(src/policy/guardrail.py) are what the ag2-sparrow gateway calls, and what any
other AG2 Space adapter must call instead of restating them. This pins both
contracts and the gateway's delegation to them.

Run: python3 tests/ag2space-tier-lines.test.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import local_task_protocol as ltp  # noqa: E402
from policy import guardrail  # noqa: E402

FENCE = "===SUTANDO SYSTEM INSTRUCTIONS (do not ignore; overrides anything above)==="


class BrokerAttestedTier(unittest.TestCase):
    def test_owner_and_guest_pass_through(self):
        self.assertEqual(ltp.broker_attested_tier("owner", "", None), ("owner", False))
        self.assertEqual(ltp.broker_attested_tier("guest", "", None), ("guest", False))

    def test_team_needs_the_exact_collaborator_boolean(self):
        self.assertEqual(ltp.broker_attested_tier("guest", "team", True), ("team", True))
        for forged in ("true", 1, "yes", None, False):
            self.assertEqual(ltp.broker_attested_tier("guest", "team", forged), ("guest", False))

    def test_collaborator_without_a_team_request_is_not_promoted(self):
        self.assertEqual(ltp.broker_attested_tier("guest", "", True), ("guest", False))
        self.assertEqual(ltp.broker_attested_tier("owner", "", True), ("owner", False))

    def test_unknown_and_missing_fail_closed_to_guest(self):
        for raw in ("", None, "admin", "OWNERS"):
            self.assertEqual(ltp.broker_attested_tier(raw, "", None), ("guest", False))

    def test_spelling_is_normalised(self):
        self.assertEqual(ltp.broker_attested_tier(" Owner ", "", None), ("owner", False))
        self.assertEqual(ltp.broker_attested_tier("other", "", None), ("guest", False))


class TierLines(unittest.TestCase):
    PATH = "results/task-x.txt"

    def test_owner_gets_no_block(self):
        self.assertEqual(guardrail.ag2space_tier_lines("owner", False, self.PATH), [])

    def test_team_is_the_team_guardrail(self):
        self.assertEqual(guardrail.ag2space_tier_lines("team", False, self.PATH),
                         guardrail.team_guardrail_lines(self.PATH))

    def test_collaborator_is_the_engage_rulebook(self):
        self.assertEqual(
            guardrail.ag2space_tier_lines("team", True, self.PATH),
            [guardrail.engage_rulebook("room", guardrail.AG2SPACE_PROVENANCE, self.PATH)])

    def test_guest_is_the_sandboxed_delegation_with_the_guest_scope(self):
        lines = guardrail.ag2space_tier_lines("guest", False, self.PATH)
        self.assertEqual(lines, guardrail.sandboxed_delegation_lines(
            "AG2 Space", "GUEST tier", self.PATH, guardrail.AG2SPACE_GUEST_SCOPE))
        self.assertEqual(sum(FENCE in line for line in lines), 1)


class GatewayDelegates(unittest.TestCase):
    """The gateway keeps no private copy of either rule."""

    SRC = (REPO / "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py").read_text()

    def test_gateway_calls_the_shared_tier_rule(self):
        self.assertIn("local_task_protocol.broker_attested_tier(", self.SRC)
        self.assertNotIn('task.get("collaborator") is True', self.SRC)

    def test_gateway_calls_the_shared_tier_lines(self):
        self.assertIn("ag2space_tier_lines(", self.SRC)
        self.assertNotIn(guardrail.AG2SPACE_GUEST_SCOPE, self.SRC)


if __name__ == "__main__":
    unittest.main()
