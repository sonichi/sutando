#!/usr/bin/env python3
"""Broker display labels can address workers without changing their identity."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills/worker-pool/scripts"
sys.path.insert(0, str(SCRIPTS))

import pool_advertise as pa  # noqa: E402
import pool_ask  # noqa: E402

import pool_route_handler  # noqa: E402
import pool_router  # noqa: E402

import pool_roster as pr  # noqa: E402
import pool_sessions  # noqa: E402

import pool_wedge_cards  # noqa: E402

W1 = "02e4302f00844397bac09533fc398248"
W2 = "212e8040d38d48b5aadab0db295dc33a"
W3 = "f45e01c820fa4a50adf34586a707bcae"
MXID = "@agent-one:ag2.space"
NEXT_MXID = "@agent-two:ag2.space"


class ProfileLabelOverrides(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ws = Path(self.temp.name)
        pr.register_worker(self.ws, W1, "base-one", runtime="codex")
        pr.register_worker(self.ws, W2, "base-two", runtime="codex")

    def frozen(self):
        return (pr.roster_path(self.ws).read_bytes(), pa.advertisement_path(self.ws).read_bytes())

    def apply(self, labels, version, mxid=MXID):
        return pr.apply_profile_label_overrides(self.ws, labels, version, mxid)

    def test_display_override_reaches_every_view_and_resolves_to_the_same_id(self):
        pr.bind_room(self.ws, "!room:ag2.space", W1)
        out = self.apply({W1: "Ryan", W2: "Codex test"}, 7)
        self.assertTrue(out["changed"])
        self.assertEqual(out["pending_worker_ids"], [])
        roster = pr.load_roster(self.ws)
        self.assertEqual(roster["worker_label_config_version"], 7)
        self.assertEqual(roster["workers"][W1]["label"], "base-one")
        self.assertEqual(roster["workers"][W1]["display_label"], "Ryan")
        self.assertEqual(pr.targets_for(roster, "!room:ag2.space"), [W1])
        self.assertEqual(pr.resolve_label(roster, "base-one"), W1)
        self.assertEqual(pr.resolve_label(roster, "Ryan"), W1)
        self.assertEqual(pr.targets_for(roster, "!unbound:ag2.space", "Ryan"), [W1])
        self.assertEqual(pool_ask.resolve(self.ws, "Ryan"), W1)
        self.assertEqual(pa.profile_workers(roster)[W1], {"label": "Ryan", "runtime": "codex"})
        ad = json.loads(pa.advertisement_path(self.ws).read_text())
        self.assertEqual(ad["profile_workers"][W1]["label"], "Ryan")
        self.assertEqual(ad["report"]["applied"]["labels"][W1], "Ryan")
        self.assertIsNone(ad["report"]["applied"]["config_version"])

        sessions = {r["worker_id"]: r for r in pool_sessions.sessions(self.ws)}
        self.assertEqual((sessions[W1]["label"], sessions[W1]["routing_label"]),
                         ("Ryan", "base-one"))
        with mock.patch.object(pool_ask.sup, "observe", return_value={}):
            by_id = {r["id"]: r for r in pool_ask.who(self.ws)}
            self.assertEqual((by_id[W1]["label"], by_id[W1]["display_label"]),
                             ("base-one", "Ryan"))
            shown = io.StringIO()
            with contextlib.redirect_stdout(shown):
                self.assertEqual(pool_ask.main(["--workspace", str(self.ws), "--who"]), 0)
        self.assertIn(f"Ryan ({W1})", shown.getvalue())
        self.assertIn("alias=base-one", shown.getvalue())
        with mock.patch.object(pool_wedge_cards.sup, "supervised_workers",
                               return_value={W1: roster["workers"][W1]}):
            self.assertEqual(pool_wedge_cards.seat_label(self.ws, W1), f"worker Ryan ({W1})")

    def test_unique_display_name_can_bind_a_room(self):
        self.apply({W1: "Ryan"}, 7)
        roster = pr.bind_room(self.ws, "!review:ag2.space", "Ryan")
        self.assertEqual(roster["bindings"]["!review:ag2.space"], W1)
        self.assertEqual(pr.load_bindings(self.ws)["!review:ag2.space"], W1)

    def test_cross_worker_name_collisions_refuse_every_routing_entry_point(self):
        cases = [({W1: "base-two"}, "base-two"),
                 ({W1: "Shared", W2: "Shared"}, "Shared")]
        for version, (labels, name) in enumerate(cases, 7):
            with self.subTest(name=name, labels=labels):
                self.apply(labels, version)
                roster = pr.load_roster(self.ws)
                with self.assertRaises(pr.AmbiguousWorkerName):
                    pr.resolve_label(roster, name)
                with self.assertRaises(pr.AmbiguousWorkerName):
                    pr.targets_for(roster, "!unbound:ag2.space", name)
                with self.assertRaisesRegex(ValueError, "more than one recipient"):
                    pool_ask.resolve(self.ws, name)
                with self.assertRaisesRegex(pool_router.RouterRefused,
                                            "more than one recipient"):
                    pool_router.route(self.ws, {"id": "task-1", "requested_worker": name})
                code, targets, _ = pool_route_handler.classify(
                    self.ws, {"id": "task-1", "requested_worker": name})
                self.assertEqual((code, targets), (pool_route_handler.MUST_HANDLE, []))
                with self.assertRaises(pr.AmbiguousWorkerName):
                    pr.bind_room(self.ws, "!review:ag2.space", name)
                self.assertEqual(pr.load_bindings(self.ws), {})

    def test_relabel_refuses_names_taken_by_another_workers_display(self):
        self.apply({W2: "Ryan"}, 7)
        with self.assertRaisesRegex(pr.RosterError, "already names worker"):
            pr.rename_worker(self.ws, W1, "Ryan")
        self.assertEqual(pr.load_roster(self.ws)["workers"][W1]["label"], "base-one")

    def test_watcher_does_not_hand_ambiguous_human_name_to_the_core(self):
        self.apply({W1: "base-two"}, 7)
        tasks = self.ws / "tasks"
        tasks.mkdir()
        task = tasks / "task-ambiguous.txt"
        task.write_text("id: task-ambiguous\nrequested_worker: base-two\ntask: work\n")
        argv = ["--task-file", str(task), "--workspace", str(self.ws)]
        self.assertEqual(pool_route_handler.main([*argv, "--probe"]),
                         pool_route_handler.MUST_HANDLE)
        self.assertEqual(pool_route_handler.main(argv), pool_route_handler.MUST_HANDLE)
        self.assertFalse((self.ws / "deliveries" / "core" / "task-ambiguous.txt").exists())
        self.assertFalse((self.ws / "deliveries" / W1 / "task-ambiguous.txt").exists())
        self.assertFalse((self.ws / "deliveries" / W2 / "task-ambiguous.txt").exists())

    def test_exact_id_and_core_work_despite_older_stored_reserved_names(self):
        for reserved, expected_code in ((W2, 0), ("core", pool_route_handler.DECLINE)):
            with self.subTest(reserved=reserved):
                roster = pr.load_roster(self.ws)
                roster["workers"][W1]["display_label"] = reserved
                pr._write_atomic(pr.roster_path(self.ws), roster)
                self.assertEqual(pr.resolve_label(roster, reserved), reserved)
                self.assertEqual(pr.targets_for(roster, "!unbound:ag2.space", reserved), [reserved])
                self.assertEqual(pool_ask.resolve(self.ws, reserved), reserved)
                code, targets, _ = pool_route_handler.classify(
                    self.ws, {"id": "task-1", "requested_worker": reserved})
                self.assertEqual(code, expected_code)
                self.assertEqual(targets, [reserved])
                pinned = pr.bind_room(self.ws, "!review:ag2.space", reserved)
                self.assertEqual(pinned["bindings"]["!review:ag2.space"], reserved)

    def test_reserved_broker_display_names_are_rejected_without_mutation(self):
        frozen = self.frozen()
        for reserved in ("core", W1, W2, W3, W2.upper()):
            with self.subTest(reserved=reserved):
                with self.assertRaisesRegex(pr.RosterError, "reserved recipient name"):
                    self.apply({W1: reserved}, 7)
                self.assertEqual(self.frozen(), frozen)

    def test_broker_display_cannot_shadow_an_existing_nonhex_worker_id(self):
        pr.register_worker(self.ws, "worker-2", "legacy-worker")
        frozen = self.frozen()
        with self.assertRaisesRegex(pr.RosterError, "reserved recipient name"):
            self.apply({W1: "worker-2"}, 7)
        self.assertEqual(self.frozen(), frozen)
        self.assertEqual(pr.resolve_label(pr.load_roster(self.ws), "worker-2"), "worker-2")

    def test_registration_cannot_shadow_an_existing_workers_human_name(self):
        self.apply({W1: "worker-2"}, 7)
        frozen = self.frozen()
        for taken in ("worker-2", "base-one"):
            with self.subTest(taken=taken):
                with mock.patch.object(pr, "publish_task_event_handler",
                                       side_effect=AssertionError("handler was published")):
                    with self.assertRaisesRegex(pr.RosterError, "conflicts with worker"):
                        pr.register_worker(self.ws, taken, "new-worker")
                self.assertEqual(self.frozen(), frozen)

    def test_removing_override_restores_base_label_and_replaying_is_byte_identical(self):
        self.apply({W1: "Ryan"}, 7)
        restored = self.apply({}, 8)
        self.assertTrue(restored["changed"])
        roster = pr.load_roster(self.ws)
        self.assertNotIn("display_label", roster["workers"][W1])
        self.assertEqual(pa.profile_workers(roster)[W1]["label"], "base-one")
        frozen = self.frozen()
        again = self.apply({}, 8)
        self.assertFalse(again["changed"])
        self.assertEqual(self.frozen(), frozen)

    def test_stale_version_cannot_revert_current_profile(self):
        self.apply({W1: "Ryan"}, 7)
        frozen = self.frozen()
        stale = self.apply({W1: "Old"}, 6)
        self.assertTrue(stale["stale"])
        self.assertEqual(self.frozen(), frozen)

    def test_same_version_repairs_local_display_label_loss(self):
        self.apply({W1: "Ryan"}, 7)
        roster = pr.load_roster(self.ws)
        roster["workers"][W1].pop("display_label")
        pr._write_atomic(pr.roster_path(self.ws), roster)
        repaired = self.apply({W1: "Ryan"}, 7)
        self.assertTrue(repaired["changed"])
        self.assertEqual(pr.load_roster(self.ws)["workers"][W1]["display_label"], "Ryan")
        self.assertEqual(pr.load_roster(self.ws)["worker_label_config_version"], 7)
        self.assertEqual(pa.profile_workers(pr.load_roster(self.ws))[W1]["label"], "Ryan")

    def test_same_version_accepts_a_new_owner_label(self):
        self.apply({W1: "Ryan"}, 7)
        updated = self.apply({W1: "Rian"}, 7)
        self.assertTrue(updated["changed"])
        roster = pr.load_roster(self.ws)
        self.assertEqual(roster["worker_label_config_version"], 7)
        self.assertEqual(roster["workers"][W1]["display_label"], "Rian")
        self.assertEqual(pa.profile_workers(roster)[W1]["label"], "Rian")

    def test_new_version_with_same_map_only_advances_watermark(self):
        self.apply({W1: "Ryan"}, 7)
        before_roster = pr.load_roster(self.ws)
        before_ad = pa.advertisement_path(self.ws).read_bytes()
        with mock.patch.object(pa, "write_advertisement", side_effect=AssertionError("republished")):
            result = self.apply({W1: "Ryan"}, 8)
        after = pr.load_roster(self.ws)
        self.assertFalse(result["changed"])
        self.assertEqual(after["version"], before_roster["version"])
        self.assertEqual(after["worker_label_config_version"], 8)
        self.assertEqual(pa.advertisement_path(self.ws).read_bytes(), before_ad)

    def test_retry_repairs_advertisement_after_publish_failure(self):
        before_ad = pa.advertisement_path(self.ws).read_bytes()
        with mock.patch.object(pa, "write_advertisement", side_effect=OSError("disk full")):
            with self.assertRaises(pr.PublishError):
                self.apply({W1: "Ryan"}, 7)
        after_failure = pr.load_roster(self.ws)
        self.assertEqual(after_failure["workers"][W1]["display_label"], "Ryan")
        self.assertEqual(pa.advertisement_path(self.ws).read_bytes(), before_ad)
        replay = self.apply({W1: "Ryan"}, 7)
        self.assertFalse(replay["changed"])
        self.assertEqual(pr.load_roster(self.ws)["version"], after_failure["version"])
        self.assertEqual(pa.profile_workers(pr.load_roster(self.ws))[W1]["label"], "Ryan")
        self.assertEqual(json.loads(pa.advertisement_path(self.ws).read_text())
                         ["profile_workers"][W1]["label"], "Ryan")

    def test_advertisement_read_failure_is_reported_on_an_unchanged_replay(self):
        self.apply({W1: "Ryan"}, 7)
        before = self.frozen()
        with mock.patch.object(pa, "ensure_advertisement", side_effect=OSError("disk offline")):
            with self.assertRaises(pr.PublishError) as raised:
                self.apply({W1: "Ryan"}, 7)
        self.assertEqual(raised.exception.roster["worker_label_config_version"], 7)
        self.assertEqual(self.frozen(), before)

    def test_reregistering_same_worker_preserves_override_at_same_version(self):
        self.apply({W1: "Ryan"}, 7)
        pr.register_worker(self.ws, W1, "new-base", runtime="codex")
        roster = pr.load_roster(self.ws)
        self.assertEqual((roster["workers"][W1]["label"],
                          roster["workers"][W1]["display_label"]), ("new-base", "Ryan"))
        again = self.apply({W1: "Ryan"}, 7)
        self.assertFalse(again["changed"])
        self.assertEqual(pr.resolve_label(pr.load_roster(self.ws), "new-base"), W1)

    def test_profile_switch_accepts_new_profiles_lower_version(self):
        self.apply({W1: "Old owner"}, 7)
        switched = self.apply({W1: "New owner"}, 1, NEXT_MXID)
        self.assertTrue(switched["changed"])
        roster = pr.load_roster(self.ws)
        self.assertEqual(roster["worker_label_profile_mxid"], NEXT_MXID)
        self.assertEqual(roster["worker_label_config_version"], 1)
        self.assertEqual(roster["workers"][W1]["display_label"], "New owner")
        stale = self.apply({W1: "Old new owner"}, 0, NEXT_MXID)
        self.assertTrue(stale["stale"])
        self.assertEqual(pr.load_roster(self.ws)["workers"][W1]["display_label"], "New owner")

    def test_profile_switch_with_unknown_worker_clears_old_cursor(self):
        self.apply({W1: "Old owner"}, 7)
        switched = self.apply({W1: "New owner", W3: "Late"}, 1, NEXT_MXID)
        self.assertEqual(switched["pending_worker_ids"], [W3])
        roster = pr.load_roster(self.ws)
        self.assertEqual(roster["worker_label_profile_mxid"], NEXT_MXID)
        self.assertNotIn("worker_label_config_version", roster)
        pr.register_worker(self.ws, W3, "new-base")
        self.apply({W1: "New owner", W3: "Late"}, 1, NEXT_MXID)
        self.assertEqual(pr.load_roster(self.ws)["worker_label_config_version"], 1)

    def test_new_profile_with_only_unknown_worker_records_source_without_a_version(self):
        before_ad = pa.advertisement_path(self.ws).read_bytes()
        result = self.apply({W3: "Late"}, 1, NEXT_MXID)
        roster = pr.load_roster(self.ws)
        self.assertFalse(result["changed"])
        self.assertEqual(result["pending_worker_ids"], [W3])
        self.assertEqual(roster["worker_label_profile_mxid"], NEXT_MXID)
        self.assertNotIn("worker_label_config_version", roster)
        self.assertEqual(pa.advertisement_path(self.ws).read_bytes(), before_ad)
        pr.register_worker(self.ws, W3, "base-three")
        self.apply({W3: "Late"}, 1, NEXT_MXID)
        self.assertEqual(pr.load_roster(self.ws)["workers"][W3]["display_label"], "Late")

    def test_retired_override_does_not_hold_the_cursor(self):
        roster = pr.load_roster(self.ws)
        roster["workers"][W2]["state"] = "retired"
        pr.compile_roster(self.ws, roster["workers"], roster["bindings"])
        applied = self.apply({W1: "Ryan", W2: "Past owner"}, 7)
        self.assertEqual(applied["pending_worker_ids"], [])
        roster = pr.load_roster(self.ws)
        self.assertEqual(roster["worker_label_config_version"], 7)
        self.assertNotIn("display_label", roster["workers"][W2])

    def test_unknown_worker_waits_without_blocking_known_labels(self):
        first = self.apply({W1: "Ryan", W3: "Late"}, 7)
        self.assertEqual(first["pending_worker_ids"], [W3])
        self.assertEqual(pr.load_roster(self.ws)["workers"][W1]["display_label"], "Ryan")
        self.assertNotIn("worker_label_config_version", pr.load_roster(self.ws))
        frozen = self.frozen()
        again = self.apply({W1: "Ryan", W3: "Late"}, 7)
        self.assertFalse(again["changed"])
        self.assertEqual(self.frozen(), frozen)
        pr.register_worker(self.ws, W3, "base-three")
        final = self.apply({W1: "Ryan", W3: "Late"}, 7)
        self.assertEqual(final["pending_worker_ids"], [])
        self.assertEqual(pr.load_roster(self.ws)["worker_label_config_version"], 7)
        self.assertEqual(pr.load_roster(self.ws)["workers"][W3]["display_label"], "Late")

    def test_invalid_snapshot_does_not_write(self):
        frozen = self.frozen()
        cases = [([], 1), ({W1: ""}, 1), ({W1: " Ryan "}, 1),
                 ({W1: "bad\nname"}, 1), ({W1: "x" * 121}, 1),
                 ({"../escape": "bad id"}, 1),
                 ({W1: "Ryan"}, True), ({W1: "Ryan"}, -1)]
        for labels, version in cases:
            with self.subTest(labels=labels, version=version):
                with self.assertRaises(pr.RosterError):
                    self.apply(labels, version)
                self.assertEqual(self.frozen(), frozen)

    def test_missing_roster_and_invalid_profile_identity_are_refused(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(pr.RosterError, "no roster"):
                pr.apply_profile_label_overrides(empty, {W1: "Ryan"}, 1, MXID)
        frozen = self.frozen()
        with self.assertRaisesRegex(pr.RosterError, "profile mxid is invalid"):
            self.apply({W1: "Ryan"}, 1, "agent-one:ag2.space")
        self.assertEqual(self.frozen(), frozen)

    def test_corrupt_label_cursor_is_refused_without_changing_the_roster(self):
        original = pr.load_roster(self.ws)
        for bad in ({"worker_label_config_version": -1},
                    {"worker_label_profile_mxid": 42}):
            with self.subTest(bad=bad):
                pr._write_atomic(pr.roster_path(self.ws), {**original, **bad})
                frozen = self.frozen()
                with self.assertRaisesRegex(pr.RosterError, "stored worker label"):
                    self.apply({W1: "Ryan"}, 1)
                self.assertEqual(self.frozen(), frozen)

    def test_other_roster_writers_keep_the_label_version_and_override(self):
        initial = pr.load_roster(self.ws)
        initial["config_version"] = 3
        pr._write_atomic(pr.roster_path(self.ws), initial)
        self.apply({W1: "Ryan"}, 7)
        pr.bind_room(self.ws, "!room:ag2.space", W1)
        pr.rename_worker(self.ws, W1, "new-base")
        pr.register_worker(self.ws, W3, "base-three")
        roster = pr.load_roster(self.ws)
        self.assertEqual(roster["config_version"], 3)
        self.assertEqual(roster["worker_label_config_version"], 7)
        self.assertEqual(roster["worker_label_profile_mxid"], MXID)
        self.assertEqual((roster["workers"][W1]["label"], roster["workers"][W1]["display_label"]),
                         ("new-base", "Ryan"))
        self.assertEqual(pr.resolve_label(roster, "new-base"), W1)
        self.assertEqual(pa.profile_workers(roster)[W1]["label"], "Ryan")

    def test_concurrent_versions_finish_at_the_newest_snapshot(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            calls = [executor.submit(pr.apply_profile_label_overrides, self.ws, labels, version, MXID)
                     for labels, version in (({W1: "First"}, 7), ({W1: "Second"}, 8))]
            for future in calls:
                future.result()
        roster = pr.load_roster(self.ws)
        self.assertEqual(roster["worker_label_config_version"], 8)
        self.assertEqual(roster["workers"][W1]["display_label"], "Second")

    def test_installed_cli_reads_stdin_and_reports_applied_version(self):
        cmd = [sys.executable, str(SCRIPTS / "apply_profile_label_overrides.py"),
               "--workspace", str(self.ws), "--config-version", "7",
               "--profile-mxid", MXID]
        run = subprocess.run(cmd, input=json.dumps({W1: "Ryan"}),
                             text=True, capture_output=True, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["worker_label_config_version"], 7)
        self.assertEqual(pa.profile_workers(pr.load_roster(self.ws))[W1]["label"], "Ryan")
        bad = subprocess.run(cmd, input="{broken", text=True, capture_output=True, check=False)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("apply-profile-label-overrides", bad.stderr)


if __name__ == "__main__":
    unittest.main()
