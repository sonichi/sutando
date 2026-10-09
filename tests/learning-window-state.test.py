#!/usr/bin/env python3
"""Exercise the real collection writer in private temporary directories."""
import copy
import importlib.util
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("window_state", ROOT / "skills/learning-window/scripts/window_state.py")
writer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(writer)


def receipt(scope="prod", rooms=("!a", "!b"), since=0, until=100):
    return {"scope": scope, "since_ms": since, "until_ms": until, "membership_count": len(rooms),
            "rooms": [{"room_id": r, "messages": [{"event_id": "$" + r, "ts": 50}], "errors": [], "coverage": "reached_cutoff"} for r in rooms]}


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state"

    def state(self):
        return json.loads((self.path / "collection-state.json").read_text())

    def test_corrupted_state_and_wrong_bootstrap_are_preserved_on_refusal(self):
        self.path.mkdir()
        path = self.path / 'collection-state.json'
        malformed = ({'schema': 2, 'scopes': {}}, {'schema': 1, 'scopes': {'prod': []}},
                     {'schema': 1, 'scopes': {'prod': {'bootstrap_ms': 10, 'rooms': {'r': {'collected_through_ms': 0}}}}})
        for value in malformed:
            path.write_text(json.dumps(value))
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                writer.plan_windows(self.path, {'prod': ['r']}, 0)
            self.assertEqual(path.read_bytes(), before)
        path.unlink()
        for memberships in ([], {'prod': [None]}):
            with self.assertRaises(ValueError):
                writer.plan_windows(self.path, memberships, 0)
            self.assertFalse(path.exists())
        writer.record_collection(self.path, 'prod', receipt(), 0)
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            writer.plan_windows(self.path, {'prod': ['!a']}, 1)
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(ValueError):
            writer.record_collection(self.path, 'prod', receipt(since=100, until=50), 0)
        self.assertEqual(path.read_bytes(), before)

    def test_receipt_persisted_before_progress_not_learning_credit(self):
        got = writer.record_collection(self.path, "prod", receipt(), 0)
        self.assertFalse(got["learning_progress_advanced"])
        digest = got["receipt_digest"]
        self.assertEqual(json.loads((self.path / "receipts" / (digest + ".json")).read_text()), receipt())
        self.assertEqual(self.state()["scopes"]["prod"]["rooms"]["!a"]["collected_through_ms"], 100)

    def test_unknown_room_retains_earliest_unread_progress(self):
        value = receipt()
        value["complete_available_history"] = True
        value["rooms"][1].update(coverage="read_failed", errors=["TimeoutError"])
        got = writer.record_collection(self.path, "prod", value, 0)
        self.assertEqual(got["pending_rooms"], ["!b"])
        self.assertNotIn("!b", self.state()["scopes"]["prod"]["rooms"])
        self.assertFalse(got["complete_available_history"])

    def test_new_room_cannot_inherit_other_room_watermark(self):
        writer.record_collection(self.path, "prod", receipt(rooms=("!a",)), 0)
        value = receipt(rooms=("!a", "!new"), since=100, until=200)
        with self.assertRaises(ValueError):
            writer.record_collection(self.path, "prod", value, 0)
        self.assertNotIn("!new", self.state()["scopes"]["prod"]["rooms"])

    def test_planner_recovers_new_rooms_and_new_scopes_from_bootstrap(self):
        writer.record_collection(self.path, "prod", receipt(rooms=("!a",)), 0)
        self.assertEqual(writer.plan_windows(self.path, {"prod": ["!a"]}, 0), {"prod": 100})
        self.assertEqual(writer.plan_windows(self.path, {"prod": ["!a", "!new"], "dev": ["!a"]}, 0), {"prod": 0, "dev": 0})

    def test_planner_keeps_failed_room_unread_and_never_changes_state(self):
        value = receipt()
        value["rooms"][1].update(coverage="page_budget_exhausted")
        writer.record_collection(self.path, "prod", value, 0)
        before = self.state()
        self.assertEqual(writer.plan_windows(self.path, {"prod": ["!a", "!b"]}, 0), {"prod": 0})
        self.assertEqual(self.state(), before)

    def test_progress_files_and_receipts_are_private(self):
        writer.record_collection(self.path, "prod", receipt(), 0)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o700)
        for path in self.path.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_scopes_never_share_progress(self):
        writer.record_collection(self.path, "prod", receipt(), 0)
        with self.assertRaises(ValueError):
            writer.record_collection(self.path, "dev", receipt(scope="dev", since=100, until=200), 0)
        self.assertNotIn("dev", self.state()["scopes"])

    def test_duplicate_replay_is_idempotent_and_old_overlap_cannot_regress(self):
        value = receipt()
        writer.record_collection(self.path, "prod", value, 0)
        before = self.state()
        writer.record_collection(self.path, "prod", value, 0)
        self.assertEqual(before, self.state())
        writer.record_collection(self.path, "prod", receipt(until=60), 0)
        self.assertEqual(self.state()["scopes"]["prod"]["rooms"]["!a"]["collected_through_ms"], 100)

    def test_failed_bundle_publication_cannot_advance_state(self):
        with patch.object(writer.os, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                writer.record_collection(self.path, "prod", receipt(), 0)
        self.assertFalse((self.path / "collection-state.json").exists())

    def test_state_failure_leaves_receipt_replayable(self):
        original = writer._atomic
        def fail(path, data):
            if path.name == "collection-state.json":
                raise OSError("interrupted after receipt")
            original(path, data)
        with patch.object(writer, "_atomic", side_effect=fail):
            with self.assertRaises(OSError):
                writer.record_collection(self.path, "prod", receipt(), 0)
        self.assertEqual(len(list((self.path / "receipts").glob("*.json"))), 1)
        self.assertFalse((self.path / "collection-state.json").exists())
        writer.record_collection(self.path, "prod", receipt(), 0)
        self.assertIn("prod", self.state()["scopes"])

    def test_concurrent_scope_writes_do_not_lose_each_other(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(writer.record_collection, self.path, scope, receipt(scope=scope), 0) for scope in ("prod", "dev")]
            for future in futures:
                future.result()
        self.assertEqual(set(self.state()["scopes"]), {"prod", "dev"})

    def test_malformed_inventory_and_state_fail_closed(self):
        for change in ({"membership_count": 77}, {"rooms": [None]}, {"scope": "other"}, {"since_ms": True}, {"until_ms": float("nan")}):
            value = receipt()
            value.update(change)
            with self.assertRaises(ValueError):
                writer.record_collection(self.path, "prod", value, 0)
        writer.record_collection(self.path, "prod", receipt(), 0)
        (self.path / "collection-state.json").write_text("{broken")
        with self.assertRaises(ValueError):
            writer.record_collection(self.path, "prod", receipt(), 0)


if __name__ == "__main__":
    unittest.main()
