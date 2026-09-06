#!/usr/bin/env python3
"""A row holding BOTH transports is the connector between its person's rows.

`durable_endpoint` returns the one route to send on, and identity used to be
built from it. A row carrying Matrix *and* Discord therefore contributed only
its Matrix endpoint, so a second Discord row for the same human shared no node,
landed in its own component, and counted as a separate reviewer -- letting ONE
person satisfy the two-reviewer minimum that exists so one person being busy
cannot stall a PR.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(Path(tempfile.mkdtemp()) / "ledger.jsonl")
_spec = importlib.util.spec_from_file_location(
    "nr", ROOT / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")
nr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nr)

# One human. The first row is HYBRID; the second is the same discord_id alone.
HYBRID = {
    "alice-mx": {"stand": "@alice:x", "room": "!r:x",
                 "discord_id": "123", "home_channel": "c1", "allowlisted": True},
    "alice-dm": {"discord_id": "123", "home_channel": "c1", "allowlisted": True},
}
TWO_PEOPLE = {
    "alice": {"stand": "@alice:x", "room": "!r:x", "allowlisted": True},
    "bob": {"stand": "@bob:x", "room": "!r:x", "allowlisted": True},
}
HYBRID_PLUS_OTHER = {
    "alice": {"stand": "@alice:x", "room": "!r:x",
              "discord_id": "1", "home_channel": "c", "allowlisted": True},
    "bob": {"discord_id": "2", "home_channel": "c", "allowlisted": True},
}


class AHybridRowIsOnePerson(unittest.TestCase):
    def test_both_routes_are_reported_for_identity(self):
        self.assertEqual(nr.durable_endpoints(HYBRID["alice-mx"]),
                         {"@alice:x", "discord:123"})

    def test_the_send_route_is_still_exactly_one_and_still_matrix(self):
        # The split exists because identity and routing are different questions;
        # widening identity must not widen where a message is actually sent.
        self.assertEqual(nr.durable_endpoint(HYBRID["alice-mx"]), "@alice:x")
        self.assertEqual(nr.durable_endpoint(HYBRID["alice-dm"]), "discord:123")

    def test_the_two_rows_are_one_component(self):
        comp = nr.identity_components(HYBRID)
        self.assertEqual(comp["alice-mx"], comp["alice-dm"])

    def test_one_person_cannot_satisfy_the_two_reviewer_minimum(self):
        targets, _worst = nr.resolve(["alice-mx", "alice-dm"], HYBRID)
        self.assertEqual(len(targets), 1, "the same human counted twice")

    def test_the_control_two_real_people_still_resolve_to_two(self):
        # Or the fix could pass by merging everything into one component.
        targets, _worst = nr.resolve(["alice", "bob"], TWO_PEOPLE)
        self.assertEqual(len(targets), 2)

    def test_the_control_a_hybrid_row_does_not_absorb_a_stranger(self):
        # A hybrid row now contributes two endpoints; neither may swallow a
        # different human who merely shares the transport.
        targets, _worst = nr.resolve(["alice", "bob"], HYBRID_PLUS_OTHER)
        self.assertEqual(len(targets), 2)

    def test_malformed_rows_still_yield_no_endpoint(self):
        for entry in ({}, {"stand": "@a:x"}, {"discord_id": "1"},
                      {"stand": ["bad"], "room": "!r:x"}, "not-a-dict"):
            self.assertEqual(nr.durable_endpoints(entry if isinstance(entry, dict) else entry),
                             set(), entry)


if __name__ == "__main__":
    unittest.main()
