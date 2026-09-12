#!/usr/bin/env python3
"""`other` is the legacy spelling of the `guest` tier: readers resolve it, writers
do not emit it. One alias table (local_task_protocol) feeds every reader, so the
policy egress guard, the gateway cap, and the observability schema map cannot
drift apart on the spelling.

Run: python3 tests/access-tier-alias.test.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import local_task_protocol as ltp  # noqa: E402

from policy.egress.result import resolve_access_tier  # noqa: E402

from observability.channel import _normalize_tier  # noqa: E402


class CanonicalAccessTier(unittest.TestCase):
    def test_legacy_other_resolves_to_guest(self):
        self.assertEqual(ltp.canonical_access_tier("other"), "guest")

    def test_guest_is_already_canonical(self):
        self.assertEqual(ltp.canonical_access_tier("guest"), "guest")

    def test_whitespace_and_case_are_not_a_different_tier(self):
        self.assertEqual(ltp.canonical_access_tier("  Other \n"), "guest")
        self.assertEqual(ltp.canonical_access_tier("OWNER"), "owner")

    def test_other_named_tiers_pass_through(self):
        for tier in ("owner", "team", "collaborator"):
            self.assertEqual(ltp.canonical_access_tier(tier), tier)

    def test_unknown_values_are_left_to_the_reader(self):
        # Readers keep their own fail-closed default; the table only unifies spelling.
        self.assertEqual(ltp.canonical_access_tier("bogus"), "bogus")
        self.assertEqual(ltp.canonical_access_tier(None), "")

    def test_the_alias_table_names_only_retired_spellings(self):
        for legacy, canonical in ltp.LEGACY_ACCESS_TIER_ALIASES.items():
            self.assertIn(canonical, ltp.ACCESS_TIERS, legacy)
            self.assertNotEqual(legacy, canonical)


class ReadersShareTheAlias(unittest.TestCase):
    def _task(self, tier_line: str) -> Path:
        p = Path(tempfile.mkdtemp()) / "task-x.txt"
        p.write_text(f"id: task-x\n{tier_line}\nsource: discord\ntask: hi\n")
        return p

    def test_egress_guard_reads_legacy_other_as_guest(self):
        self.assertEqual(resolve_access_tier(self._task("access_tier: other")), "guest")

    def test_egress_guard_reads_guest_as_guest(self):
        self.assertEqual(resolve_access_tier(self._task("access_tier: guest")), "guest")

    def test_egress_guard_treats_other_and_guest_as_one_tier_not_a_conflict(self):
        # Two spellings of the same tier are not an injection conflict.
        p = self._task("access_tier: other\naccess_tier: guest")
        self.assertEqual(resolve_access_tier(p), "guest")

    def test_observability_maps_guest_and_legacy_other_to_public(self):
        self.assertEqual(_normalize_tier("guest"), "public")
        self.assertEqual(_normalize_tier("other"), "public")


class DiscordBridgeEmitsGuest(unittest.TestCase):
    """The Discord bridge's rulebook table is keyed on the canonical name."""

    def test_the_guest_rulebook_exists_and_the_other_key_is_gone(self):
        src = (REPO / "src" / "discord-bridge.py").read_text()
        table = src[src.index("tier_instructions = {"):src.index("tier_instructions.get(")]
        self.assertIn('"guest": (', table)
        self.assertNotIn('"other": (', table)
        self.assertIn("GUEST tier sender", table)
        self.assertNotIn("OTHER tier sender", table)

    def test_the_sandbox_fallback_rulebook_is_guest(self):
        src = (REPO / "src" / "discord-bridge.py").read_text()
        self.assertIn("tier_instructions['guest']", src)
        self.assertNotIn("tier_instructions['other']", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
