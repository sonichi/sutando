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
        # Nothing to name a person by. The stand-only and discord-only rows moved
        # to the next test: not malformed, unroutable.
        for entry in ({}, {"stand": ["bad"], "room": "!r:x"},
                      {"discord_id": ["bad"], "home_channel": "c"}, "not-a-dict"):
            self.assertEqual(nr.durable_endpoints(entry if isinstance(entry, dict) else entry),
                             set(), entry)

    def test_an_unroutable_row_still_names_its_person(self):
        """IDENTITY is what a row declares; ROUTABILITY is whether we can send.

        Coupling them made one person read as two whenever the transport we were
        not sending on had incomplete routing metadata, and two rows for that
        person then satisfied the two-reviewer minimum.
        """
        for entry, endpoint in (({"stand": "@a:x"}, "@a:x"),
                                ({"discord_id": "1"}, "discord:1")):
            self.assertEqual(nr.durable_endpoints(entry), {endpoint},
                             f"{entry} names a person even with no route")
            self.assertIsNone(nr.durable_endpoint(entry),
                              f"{entry} has no complete route to send on")

    def test_the_partial_hybrid_is_one_person_not_two(self):
        """The reviewer's case: both rows routable, same human, one missing
        `home_channel` on the transport it does not send on."""
        roster = {
            "mx": {"stand": "@alice:x", "room": "!r:x", "discord_id": "123",
                   "allowlisted": True},
            "dc": {"discord_id": "123", "home_channel": "c1", "allowlisted": True},
        }
        targets, _worst = nr.resolve(["mx", "dc"], roster)
        self.assertEqual(len(targets), 1,
                         "a missing home_channel on the unused route split one human in two")

    def test_the_symmetric_case_a_missing_room_also_joins(self):
        roster = {
            "mx": {"stand": "@alice:x", "discord_id": "123", "home_channel": "c1",
                   "allowlisted": True},
            "dc": {"discord_id": "123", "home_channel": "c1", "allowlisted": True},
        }
        targets, _worst = nr.resolve(["mx", "dc"], roster)
        self.assertEqual(len(targets), 1, "a missing room must not split one human either")

    def test_the_control_a_partial_row_does_not_absorb_a_different_person(self):
        roster = {
            "mx": {"stand": "@alice:x", "room": "!r:x", "discord_id": "123",
                   "allowlisted": True},
            "dc": {"discord_id": "999", "home_channel": "c1", "allowlisted": True},
        }
        targets, _worst = nr.resolve(["mx", "dc"], roster)
        self.assertEqual(len(targets), 2,
                         "loosening identity must not merge two different humans")


if __name__ == "__main__":
    unittest.main()
