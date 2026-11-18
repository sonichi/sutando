#!/usr/bin/env python3
"""roster_membership: the one rule for which roster workers count, and each caller delegating to it.

Run: python3 tests/roster-membership.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "worker-pool" / "scripts"))
import roster_membership
import seat_inbox
import health_snapshot
import pool_roster


class ContractTests(unittest.TestCase):
    def test_only_a_mapping_row_whose_state_is_retired_is_out(self):
        self.assertFalse(roster_membership.is_member({"state": "retired"}))
        for row in ({"state": "live"}, {"state": "recovering"}, {}, {"state": "Retired"}, None, "retired", []):
            self.assertTrue(roster_membership.is_member(row), row)

    def test_members_are_the_keys_of_member_rows(self):
        workers = {"a": {"state": "live"}, "b": {"state": "retired"}, "c": None, "d": {}}
        self.assertEqual(roster_membership.members(workers), {"a", "c", "d"})
        self.assertEqual(roster_membership.members({}), set())

    def test_a_non_mapping_is_the_caller_to_handle(self):
        with self.assertRaises(AttributeError):
            roster_membership.members(["a"])


class DelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "state").mkdir()
        (self.ws / "deliveries" / "planted").mkdir(parents=True)
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"workers": {"real": {"state": "live"}, "gone": {"state": "retired"}}}))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_seat_inbox_takes_membership_from_the_rule(self):
        with mock.patch.object(roster_membership, "members", return_value={"planted"}) as rule, \
             mock.patch("connections.own_worker_homes", return_value=[]):
            got = seat_inbox.seat(self.ws / "deliveries" / "planted", self.ws)
        self.assertEqual(got["worker"], "planted")
        rule.assert_called_once_with({"real": {"state": "live"}, "gone": {"state": "retired"}})

    def test_pool_roster_unknown_targets_takes_membership_from_the_rule(self):
        roster = {"workers": {"real": {"state": "live"}, "gone": {"state": "retired"}}}
        with mock.patch.object(roster_membership, "members", return_value={"gone"}) as rule:
            got = pool_roster.unknown_targets(roster, ["real", "gone", "core"])
        self.assertEqual(got, ["real"])
        rule.assert_called_once_with(roster["workers"])

    def test_health_snapshot_workers_take_membership_from_the_rule(self):
        with mock.patch.object(roster_membership, "is_member", side_effect=lambda row: row.get("state") == "retired"):
            got = health_snapshot._workers(self.ws)
        self.assertEqual([w[0] for w in got], ["gone"])


if __name__ == "__main__":
    unittest.main()
