#!/usr/bin/env python3
"""Durable logical session profiles: identity, seat fencing, room policy.

One test per invariant the profile store has to hold — profile identity
outliving both the core and the provider session, the lead as sole writer of
seating, epoch fencing against a revived stale core, generation rotation that
preserves a failed lineage, and fail-closed corruption handling.

Run: python3 tests/pool-session-profiles.test.py   (stdlib only)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from pool_profiles import (  # noqa: E402
    DEFAULT_CONTEXT_POLICY, ProfileStore, ProfileStoreCorrupt, NotTheWriter,
    PolicyViolation, SeatFenced, UnknownProfile)

LEAD = "pool-lead"
ROOMS = {"!room-a:ag2.space": {"read": True, "write": "scoped"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "pool" / "profiles.json"
        self.clock = [1000.0]
        self.store = ProfileStore(self.path, lead_label=LEAD,
                                  now_fn=lambda: self.clock[0])
        self.addCleanup(self.tmp.cleanup)

    def make(self, pid="pro-main", rooms=None, runtime="claude"):
        return self.store.create(pid, rooms or ROOMS, runtime, writer=LEAD)


class IdentityTests(Base):
    def test_new_profile_starts_unseated_at_generation_one(self):
        prof = self.make()
        self.assertEqual(prof["lineage"]["generation"], 1)
        self.assertIsNone(prof["lineage"]["active_session_id"])
        self.assertEqual(prof["seat"], {"core_id": None, "epoch": 0})

    def test_identity_survives_core_and_session_replacement(self):
        self.make()
        e1 = self.store.seat("pro-main", "core-1", writer=LEAD)
        self.store.advance_session("pro-main", "core-1", e1, "sess-A")
        e2 = self.store.reseat("pro-main", "core-2", writer=LEAD)
        self.store.advance_session("pro-main", "core-2", e2, "sess-B")
        prof = self.store.get("pro-main")
        self.assertEqual(prof["rooms"], ROOMS)
        self.assertEqual(prof["seat"]["core_id"], "core-2")
        self.assertEqual(prof["lineage"]["active_session_id"], "sess-B")

    def test_a_room_resolves_to_its_profile(self):
        self.make()
        self.assertEqual(
            self.store.profile_for_room("!room-a:ag2.space"), "pro-main")
        self.assertIsNone(self.store.profile_for_room("!elsewhere:ag2.space"))

    def test_unknown_profile_is_an_error_not_an_empty_dict(self):
        with self.assertRaises(UnknownProfile):
            self.store.get("nope")


class SeatWriterTests(Base):
    def test_only_the_lead_may_seat(self):
        self.make()
        for op in (lambda: self.store.seat("pro-main", "core-1",
                                           writer="core-1"),
                   lambda: self.store.unseat("pro-main", writer="core-1"),
                   lambda: self.store.reseat("pro-main", "core-2",
                                             writer="core-2")):
            with self.assertRaises(NotTheWriter):
                op()
        self.assertIsNone(self.store.get("pro-main")["seat"]["core_id"])

    def test_only_the_lead_may_create_or_change_membership(self):
        with self.assertRaises(NotTheWriter):
            self.store.create("x", ROOMS, "claude", writer="core-1")
        self.make()
        with self.assertRaises(NotTheWriter):
            self.store.set_rooms("pro-main", ROOMS, writer="core-1")

    def test_every_seating_change_bumps_the_epoch(self):
        self.make()
        seen = [self.store.seat("pro-main", "core-1", writer=LEAD),
                self.store.unseat("pro-main", writer=LEAD),
                self.store.seat("pro-main", "core-2", writer=LEAD)]
        self.assertEqual(seen, [1, 2, 3])

    def test_reseat_bumps_exactly_once(self):
        self.make()
        e1 = self.store.seat("pro-main", "core-1", writer=LEAD)
        e2 = self.store.reseat("pro-main", "core-2", writer=LEAD)
        self.assertEqual(e2, e1 + 1)


class FencingTests(Base):
    def test_a_revived_stale_core_cannot_advance_the_lineage(self):
        self.make()
        stale = self.store.seat("pro-main", "core-1", writer=LEAD)
        self.store.advance_session("pro-main", "core-1", stale, "sess-A")
        self.store.reseat("pro-main", "core-2", writer=LEAD)
        with self.assertRaises(SeatFenced):
            self.store.advance_session("pro-main", "core-1", stale, "sess-ZOMBIE")
        self.assertEqual(
            self.store.get("pro-main")["lineage"]["active_session_id"], "sess-A")

    def test_the_right_core_with_a_stale_epoch_is_still_fenced(self):
        self.make()
        old = self.store.seat("pro-main", "core-1", writer=LEAD)
        self.store.unseat("pro-main", writer=LEAD)
        self.store.seat("pro-main", "core-1", writer=LEAD)
        with self.assertRaises(SeatFenced):
            self.store.advance_session("pro-main", "core-1", old, "sess-B")

    def test_an_unseated_profile_accepts_no_lineage_write(self):
        self.make()
        e1 = self.store.seat("pro-main", "core-1", writer=LEAD)
        self.store.unseat("pro-main", writer=LEAD)
        with self.assertRaises(SeatFenced):
            self.store.advance_session("pro-main", "core-1", e1, "sess-A")

    def test_rotate_is_fenced_too(self):
        self.make()
        stale = self.store.seat("pro-main", "core-1", writer=LEAD)
        self.store.reseat("pro-main", "core-2", writer=LEAD)
        with self.assertRaises(SeatFenced):
            self.store.rotate("pro-main", "core-1", stale, "rotated_for_size")
        self.assertEqual(self.store.get("pro-main")["lineage"]["generation"], 1)


class LineageTests(Base):
    def test_rotation_preserves_the_closed_session(self):
        self.make()
        e = self.store.seat("pro-main", "core-1", writer=LEAD)
        self.store.advance_session("pro-main", "core-1", e, "sess-A")
        self.clock[0] = 2000.0
        gen = self.store.rotate("pro-main", "core-1", e, "resume_failed")
        lin = self.store.get("pro-main")["lineage"]
        self.assertEqual((gen, lin["generation"]), (2, 2))
        self.assertIsNone(lin["active_session_id"])
        self.assertEqual(lin["previous_session_ids"], [{
            "session_id": "sess-A", "generation": 1,
            "reason": "resume_failed", "ended_at": 2000.0}])

    def test_a_chain_of_rotations_keeps_every_ancestor_in_order(self):
        self.make()
        e = self.store.seat("pro-main", "core-1", writer=LEAD)
        for n, reason in ((1, "rotated_for_size"), (2, "crashed"),
                          (3, "auth_death")):
            self.store.advance_session("pro-main", "core-1", e, f"sess-{n}")
            self.store.rotate("pro-main", "core-1", e, reason)
        lin = self.store.get("pro-main")["lineage"]
        self.assertEqual([p["session_id"] for p in lin["previous_session_ids"]],
                         ["sess-1", "sess-2", "sess-3"])
        self.assertEqual([p["generation"] for p in lin["previous_session_ids"]],
                         [1, 2, 3])
        self.assertEqual(lin["generation"], 4)

    def test_rotating_an_unstarted_generation_records_no_ancestor(self):
        self.make()
        e = self.store.seat("pro-main", "core-1", writer=LEAD)
        self.store.rotate("pro-main", "core-1", e, "manual")
        lin = self.store.get("pro-main")["lineage"]
        self.assertEqual(lin["previous_session_ids"], [])
        self.assertEqual(lin["generation"], 2)

    def test_an_unenumerated_rotate_reason_is_refused(self):
        self.make()
        e = self.store.seat("pro-main", "core-1", writer=LEAD)
        with self.assertRaises(PolicyViolation):
            self.store.rotate("pro-main", "core-1", e, "because")


class RoomPolicyTests(Base):
    def test_a_room_needs_both_read_and_write_declared(self):
        for bad in ({"!r:x": {}}, {"!r:x": {"read": True}},
                    {"!r:x": {"write": "scoped"}}):
            with self.assertRaises(PolicyViolation):
                self.store.create("p", bad, "claude", writer=LEAD)

    def test_an_unenumerated_write_mode_is_refused(self):
        with self.assertRaises(PolicyViolation):
            self.store.create("p", {"!r:x": {"read": True, "write": "full"}},
                              "claude", writer=LEAD)

    def test_an_unknown_room_key_is_refused_rather_than_ignored(self):
        with self.assertRaises(PolicyViolation):
            self.store.create("p", {"!r:x": {"read": True, "write": "scoped",
                                             "share_all": True}},
                              "claude", writer=LEAD)

    def test_context_policy_defaults_are_explicit_and_provenance_preserving(self):
        prof = self.make()
        self.assertEqual(prof["context_policy"], DEFAULT_CONTEXT_POLICY)

    def test_an_unenumerated_sharing_mode_is_refused(self):
        with self.assertRaises(PolicyViolation):
            self.store.create("p", ROOMS, "claude", writer=LEAD,
                              context_policy={"sharing": "implicit",
                                              "provenance": "preserve_room"})

    def test_adding_a_room_revalidates_the_whole_set(self):
        self.make()
        with self.assertRaises(PolicyViolation):
            self.store.set_rooms("pro-main", {
                "!room-a:ag2.space": {"read": True, "write": "scoped"},
                "!customer:ag2.space": {"read": True, "write": "everywhere"},
            }, writer=LEAD)
        self.assertEqual(list(self.store.get("pro-main")["rooms"]),
                         ["!room-a:ag2.space"])

    def test_a_valid_multi_room_group_is_accepted(self):
        self.make()
        rooms = {"!room-a:ag2.space": {"read": True, "write": "scoped"},
                 "!room-b:ag2.space": {"read": True, "write": "none"}}
        self.store.set_rooms("pro-main", rooms, writer=LEAD)
        self.assertEqual(self.store.get("pro-main")["rooms"], rooms)
        self.assertEqual(
            self.store.profile_for_room("!room-b:ag2.space"), "pro-main")

    def test_an_unsupported_runtime_is_refused(self):
        with self.assertRaises(PolicyViolation):
            self.store.create("p", ROOMS, "gemini", writer=LEAD)


class CorruptionTests(Base):
    def test_a_missing_store_is_empty_not_an_error(self):
        self.assertEqual(self.store.load()["profiles"], {})

    def test_unparseable_json_fails_closed(self):
        self.make()
        self.path.write_text("{not json")
        with self.assertRaises(ProfileStoreCorrupt):
            self.store.load()

    def test_a_corrupt_store_is_never_replaced_by_an_empty_one(self):
        self.make()
        self.path.write_text("{not json")
        with self.assertRaises(ProfileStoreCorrupt):
            self.store.seat("pro-main", "core-1", writer=LEAD)
        self.assertEqual(self.path.read_text(), "{not json")

    def test_a_wrong_schema_version_fails_closed(self):
        self.make()
        data = json.loads(self.path.read_text())
        data["version"] = 99
        self.path.write_text(json.dumps(data))
        with self.assertRaises(ProfileStoreCorrupt):
            self.store.load()

    def test_a_profile_missing_its_seat_fails_closed(self):
        self.make()
        data = json.loads(self.path.read_text())
        del data["profiles"]["pro-main"]["seat"]
        self.path.write_text(json.dumps(data))
        with self.assertRaises(ProfileStoreCorrupt):
            self.store.load()

    def test_no_temp_file_survives_a_successful_write(self):
        self.make()
        self.store.seat("pro-main", "core-1", writer=LEAD)
        leftovers = [p.name for p in self.path.parent.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


SEAT_CHILD = """
import sys
sys.path.insert(0, {src!r})
from pool_profiles import ProfileStore
ProfileStore({path!r}, lead_label="pool-lead").seat("pro-main", "core-%d", writer="pool-lead")
"""


class ConcurrencyTests(Base):
    def test_concurrent_seats_lose_no_epoch(self):
        """Real processes against the production writer — a lost update would
        show as a final epoch below the number of successful seats."""
        self.make()
        n = 8
        procs = [subprocess.Popen(
            [sys.executable, "-c",
             SEAT_CHILD.format(src=str(REPO / "src"), path=str(self.path)) % i])
            for i in range(n)]
        rcs = [p.wait() for p in procs]
        self.assertEqual(rcs, [0] * n, "a child writer failed")
        self.assertEqual(self.store.get("pro-main")["seat"]["epoch"], n)


class BoundaryTests(Base):
    def test_the_store_carries_no_task_ownership_logic(self):
        """Task ownership stays with the atomic-rename claim; this module must
        not grow a second authority for it."""
        import pool_profiles
        names = [n for n in dir(pool_profiles) if not n.startswith("_")]
        self.assertEqual(
            [n for n in names if "task" in n.lower() or "claim" in n.lower()],
            [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
