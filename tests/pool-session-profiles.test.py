#!/usr/bin/env python3
"""Durable logical session profiles: identity, seat fencing, generation graph.

One test per invariant — identity outliving both the core and the provider
session, the lead as sole writer of seating, epoch fencing against a revived
stale core, a head that advances only when a child actually starts, ancestry
as a parent-linked graph, room/context policy enumeration, fail-closed
corruption, and lost-update safety across the atomic replace.

Run: python3 tests/pool-session-profiles.test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from pool_profiles import (  # noqa: E402
    DEFAULT_CONTEXT_POLICY, HeadMoved, NotTheWriter, PolicyViolation,
    ProfileStore, ProfileStoreCorrupt, SeatFenced, UnknownGeneration,
    UnknownProfile)

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

    def make(self, pid="pro-main", rooms=None):
        return self.store.create(pid, rooms or ROOMS, writer=LEAD)

    def seated(self, pid="pro-main", core="core-1"):
        return self.store.seat(pid, core, writer=LEAD)

    def started(self, pid="pro-main", core="core-1", epoch=None,
                session="sess-A", runtime="claude", reason="initial"):
        """begin + promote — the ordinary path a core takes on startup."""
        e = self.seated(pid, core) if epoch is None else epoch
        gid = self.store.begin_generation(pid, core, e, runtime, reason)
        self.store.promote_generation(pid, core, e, gid, session)
        return e, gid


class IdentityTests(Base):
    def test_a_new_profile_is_unseated_and_headless(self):
        prof = self.make()
        self.assertEqual(prof["seat"], {"core_id": None, "epoch": 0})
        self.assertIsNone(prof["head_generation_id"])
        self.assertEqual(prof["generations"], {})

    def test_identity_survives_core_and_session_replacement(self):
        self.make()
        e1, _ = self.started(session="sess-A")
        e2 = self.store.reseat("pro-main", "core-2", writer=LEAD)
        gid = self.store.begin_generation("pro-main", "core-2", e2, "claude",
                                          "crashed")
        self.store.promote_generation("pro-main", "core-2", e2, gid, "sess-B")
        prof = self.store.get("pro-main")
        self.assertEqual(prof["rooms"], ROOMS)
        self.assertEqual(prof["seat"]["core_id"], "core-2")
        self.assertEqual(self.store.head("pro-main")["session_id"], "sess-B")

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
            self.store.create("x", ROOMS, writer="core-1")
        self.make()
        with self.assertRaises(NotTheWriter):
            self.store.set_rooms("pro-main", ROOMS, writer="core-1")

    def test_every_seating_change_bumps_the_epoch(self):
        self.make()
        self.assertEqual([self.store.seat("pro-main", "core-1", writer=LEAD),
                          self.store.unseat("pro-main", writer=LEAD),
                          self.store.seat("pro-main", "core-2", writer=LEAD)],
                         [1, 2, 3])

    def test_reseat_bumps_exactly_once(self):
        self.make()
        e1 = self.seated()
        self.assertEqual(self.store.reseat("pro-main", "core-2", writer=LEAD),
                         e1 + 1)


class FencingTests(Base):
    def test_a_revived_stale_core_cannot_begin_a_generation(self):
        self.make()
        stale, _ = self.started(session="sess-A")
        self.store.reseat("pro-main", "core-2", writer=LEAD)
        with self.assertRaises(SeatFenced):
            self.store.begin_generation("pro-main", "core-1", stale, "claude",
                                        "crashed")
        self.assertEqual(self.store.head("pro-main")["session_id"], "sess-A")

    def test_a_revived_stale_core_cannot_promote(self):
        self.make()
        e = self.seated()
        gid = self.store.begin_generation("pro-main", "core-1", e, "claude",
                                          "initial")
        self.store.reseat("pro-main", "core-2", writer=LEAD)
        with self.assertRaises(SeatFenced):
            self.store.promote_generation("pro-main", "core-1", e, gid, "zomb")
        self.assertIsNone(self.store.get("pro-main")["head_generation_id"])

    def test_the_right_core_with_a_stale_epoch_is_still_fenced(self):
        self.make()
        old = self.seated()
        self.store.unseat("pro-main", writer=LEAD)
        self.seated()
        with self.assertRaises(SeatFenced):
            self.store.begin_generation("pro-main", "core-1", old, "claude",
                                        "initial")

    def test_an_unseated_profile_accepts_no_lineage_write(self):
        self.make()
        e = self.seated()
        self.store.unseat("pro-main", writer=LEAD)
        with self.assertRaises(SeatFenced):
            self.store.begin_generation("pro-main", "core-1", e, "claude",
                                        "initial")


class HeadAdvanceTests(Base):
    def test_begin_does_not_move_the_head(self):
        self.make()
        e, _ = self.started(session="sess-A")
        head_before = self.store.get("pro-main")["head_generation_id"]
        self.store.begin_generation("pro-main", "core-1", e, "claude",
                                    "rotated_for_size")
        self.assertEqual(self.store.get("pro-main")["head_generation_id"],
                         head_before)
        self.assertEqual(self.store.head("pro-main")["session_id"], "sess-A")

    def test_a_failed_child_leaves_the_previous_head_recoverable(self):
        """The window this whole state machine exists for: a session that
        never starts must not cost the profile its recoverable generation."""
        self.make()
        e, g1 = self.started(session="sess-A")
        g2 = self.store.begin_generation("pro-main", "core-1", e, "claude",
                                         "rotated_for_size")
        self.store.fail_generation("pro-main", "core-1", e, g2, "resume_failed")
        prof = self.store.get("pro-main")
        self.assertEqual(prof["head_generation_id"], g1)
        self.assertEqual(prof["generations"][g1]["status"], "active")
        self.assertEqual(prof["generations"][g1]["session_id"], "sess-A")
        self.assertEqual(prof["generations"][g2]["status"], "failed")
        self.assertEqual(prof["generations"][g2]["transition_reason"],
                         "resume_failed")

    def test_a_retry_after_a_failed_child_parents_off_the_live_head(self):
        self.make()
        e, g1 = self.started(session="sess-A")
        g2 = self.store.begin_generation("pro-main", "core-1", e, "claude",
                                         "rotated_for_size")
        self.store.fail_generation("pro-main", "core-1", e, g2, "resume_failed")
        g3 = self.store.begin_generation("pro-main", "core-1", e, "claude",
                                         "rotated_for_size")
        self.store.promote_generation("pro-main", "core-1", e, g3, "sess-C")
        prof = self.store.get("pro-main")
        self.assertEqual(prof["generations"][g3]["parent_generation_id"], g1)
        self.assertEqual(prof["head_generation_id"], g3)
        self.assertEqual(prof["generations"][g1]["status"], "superseded")

    def test_promotion_is_refused_when_the_head_moved_underneath(self):
        self.make()
        e, g1 = self.started(session="sess-A")
        stale = self.store.begin_generation("pro-main", "core-1", e, "claude",
                                            "rotated_for_size")
        winner = self.store.begin_generation("pro-main", "core-1", e, "claude",
                                             "rotated_for_size")
        self.store.promote_generation("pro-main", "core-1", e, winner, "sess-W")
        with self.assertRaises(HeadMoved):
            self.store.promote_generation("pro-main", "core-1", e, stale,
                                          "sess-L")
        self.assertEqual(self.store.get("pro-main")["head_generation_id"],
                         winner)

    def test_a_generation_cannot_be_promoted_twice(self):
        self.make()
        e, g1 = self.started(session="sess-A")
        with self.assertRaises(HeadMoved):
            self.store.promote_generation("pro-main", "core-1", e, g1, "again")

    def test_a_failed_generation_cannot_be_promoted(self):
        self.make()
        e = self.seated()
        g = self.store.begin_generation("pro-main", "core-1", e, "claude",
                                        "initial")
        self.store.fail_generation("pro-main", "core-1", e, g, "crashed")
        with self.assertRaises(HeadMoved):
            self.store.promote_generation("pro-main", "core-1", e, g, "s")

    def test_promoting_an_unknown_generation_is_an_error(self):
        self.make()
        e = self.seated()
        with self.assertRaises(UnknownGeneration):
            self.store.promote_generation("pro-main", "core-1", e, "g99", "s")


class AncestryTests(Base):
    def test_ancestry_is_head_first_through_parent_links(self):
        self.make()
        e, g1 = self.started(session="sess-1")
        ids = [g1]
        for n, reason in ((2, "rotated_for_size"), (3, "crashed"),
                          (4, "auth_death")):
            g = self.store.begin_generation("pro-main", "core-1", e, "claude",
                                            reason)
            self.store.promote_generation("pro-main", "core-1", e, g,
                                          f"sess-{n}")
            ids.append(g)
        self.assertEqual(self.store.ancestry("pro-main"), list(reversed(ids)))

    def test_a_branch_off_an_older_generation_is_expressible(self):
        """A list could not say this: g3's parent is g1, not the head."""
        self.make()
        e, g1 = self.started(session="sess-1")
        g2 = self.store.begin_generation("pro-main", "core-1", e, "claude",
                                         "rotated_for_size")
        self.store.promote_generation("pro-main", "core-1", e, g2, "sess-2")
        g3 = self.store.begin_generation("pro-main", "core-1", e, "claude",
                                         "branch", parent_generation_id=g1)
        prof = self.store.get("pro-main")
        self.assertEqual(prof["generations"][g3]["parent_generation_id"], g1)
        # head is still g2; a branch whose parent is not the head cannot take it
        with self.assertRaises(HeadMoved):
            self.store.promote_generation("pro-main", "core-1", e, g3, "sess-3")

    def test_a_runtime_switch_is_a_generation_not_a_new_profile(self):
        self.make()
        e, g1 = self.started(session="sess-A", runtime="claude")
        g2 = self.store.begin_generation("pro-main", "core-1", e, "codex",
                                         "runtime_switch")
        self.store.promote_generation("pro-main", "core-1", e, g2, "thread-1")
        prof = self.store.get("pro-main")
        self.assertEqual(prof["generations"][g1]["runtime"], "claude")
        self.assertEqual(prof["generations"][g2]["runtime"], "codex")
        self.assertEqual(self.store.ancestry("pro-main"), [g2, g1])

    def test_per_generation_refs_have_somewhere_to_live(self):
        self.make()
        e, g1 = self.started(session="sess-A")
        self.store.annotate_generation(
            "pro-main", "core-1", e, g1, transcript_ref="/t/sess-A.jsonl",
            digest_ref="/d/sess-A.md",
            room_watermarks={"!room-a:ag2.space": "$evt1"})
        gen = self.store.get("pro-main")["generations"][g1]
        self.assertEqual(gen["transcript_ref"], "/t/sess-A.jsonl")
        self.assertEqual(gen["digest_ref"], "/d/sess-A.md")
        self.assertEqual(gen["room_watermarks"], {"!room-a:ag2.space": "$evt1"})

    def test_an_unenumerated_transition_reason_is_refused(self):
        self.make()
        e = self.seated()
        with self.assertRaises(PolicyViolation):
            self.store.begin_generation("pro-main", "core-1", e, "claude",
                                        "because")

    def test_an_unenumerated_runtime_is_refused(self):
        self.make()
        e = self.seated()
        with self.assertRaises(PolicyViolation):
            self.store.begin_generation("pro-main", "core-1", e, "gemini",
                                        "initial")


class RoomPolicyTests(Base):
    def test_a_room_needs_both_read_and_write_declared(self):
        for bad in ({"!r:x": {}}, {"!r:x": {"read": True}},
                    {"!r:x": {"write": "scoped"}}):
            with self.assertRaises(PolicyViolation):
                self.store.create("p", bad, writer=LEAD)

    def test_an_unenumerated_write_mode_is_refused(self):
        with self.assertRaises(PolicyViolation):
            self.store.create("p", {"!r:x": {"read": True, "write": "full"}},
                              writer=LEAD)

    def test_an_unknown_room_key_is_refused_rather_than_ignored(self):
        with self.assertRaises(PolicyViolation):
            self.store.create("p", {"!r:x": {"read": True, "write": "scoped",
                                             "share_all": True}}, writer=LEAD)

    def test_context_policy_defaults_are_explicit_and_provenance_preserving(self):
        self.assertEqual(self.make()["context_policy"], DEFAULT_CONTEXT_POLICY)

    def test_an_unenumerated_sharing_mode_is_refused(self):
        with self.assertRaises(PolicyViolation):
            self.store.create("p", ROOMS, writer=LEAD,
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

    def test_a_head_naming_no_generation_fails_closed(self):
        self.make()
        data = json.loads(self.path.read_text())
        data["profiles"]["pro-main"]["head_generation_id"] = "g404"
        self.path.write_text(json.dumps(data))
        with self.assertRaises(ProfileStoreCorrupt):
            self.store.load()

    def test_a_generation_with_a_bad_status_fails_closed(self):
        self.make()
        self.started()
        data = json.loads(self.path.read_text())
        data["profiles"]["pro-main"]["generations"]["g1"]["status"] = "vibes"
        self.path.write_text(json.dumps(data))
        with self.assertRaises(ProfileStoreCorrupt):
            self.store.load()

    def test_no_temp_file_survives_a_successful_write(self):
        self.make()
        self.seated()
        self.assertEqual([p.name for p in self.path.parent.iterdir()
                          if p.name.endswith(".tmp")], [])


class LockingTests(Base):
    def test_the_lock_is_a_sidecar_the_writer_never_replaces(self):
        """Locking the data file would let a waiter inherit the pre-replace
        inode and clobber a newer one; the lock inode must never change."""
        self.make()
        lock, data = self.store._lock_path(), self.path
        self.assertNotEqual(lock, data)
        lock_ino, data_ino = os.stat(lock).st_ino, os.stat(data).st_ino
        for i in range(3):
            self.store.seat("pro-main", f"core-{i}", writer=LEAD)
        self.assertEqual(os.stat(lock).st_ino, lock_ino,
                         "lock inode moved — it is being replaced")
        self.assertNotEqual(os.stat(data).st_ino, data_ino,
                            "data inode never moved — replace is not exercised")


SEAT_CHILD = """
import sys
sys.path.insert(0, {src!r})
from pool_profiles import ProfileStore
p = {path!r}
# Open the data path BEFORE contending: a writer that read outside the lock,
# or locked the data inode, would apply onto this pre-replace snapshot.
try:
    stale = open(p, "rb").read()
except OSError:
    stale = b""
s = ProfileStore(p, lead_label="pool-lead")
for i in range({ops}):
    s.seat("pro-main", "core-%d" % i, writer="pool-lead")
"""


class ConcurrencyTests(Base):
    def test_no_seat_is_lost_across_the_atomic_replace(self):
        """Real processes through the production writer, each holding a
        pre-replace snapshot before it contends. A lost update shows as a
        final epoch below the number of successful seats."""
        self.make()
        procs, ops = 8, 10
        children = [subprocess.Popen(
            [sys.executable, "-c",
             SEAT_CHILD.format(src=str(REPO / "src"), path=str(self.path),
                               ops=ops)])
            for _ in range(procs)]
        rcs = [c.wait() for c in children]
        self.assertEqual(rcs, [0] * procs, "a child writer failed")
        self.assertEqual(self.store.get("pro-main")["seat"]["epoch"],
                         procs * ops)


class BoundaryTests(Base):
    def test_the_store_imports_no_task_or_claim_module(self):
        """Task ownership stays with the atomic-rename claim. An import
        boundary is the check; a name scan would miss an aliased import."""
        src = REPO / "src"
        out = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(src)!r});"
             " import pool_profiles, json;"
             " print(json.dumps(sorted(m for m in sys.modules"
             " if 'task' in m or 'claim' in m or 'pool_' in m)))"],
            check=True, capture_output=True, text=True).stdout
        self.assertEqual(json.loads(out), ["pool_profiles"])

    def test_the_store_touches_no_path_but_its_own(self):
        self.make()
        self.started()
        written = sorted(p.name for p in self.path.parent.iterdir())
        self.assertEqual(written, ["profiles.json", "profiles.json.lock"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
