#!/usr/bin/env python3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/learning-window/scripts"))
import collection_pass
sys.path.insert(0, str(ROOT / "skills/agent-room-ops"))
from history_collection import collect_window


class PassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "state"
        self.calls = []

    def collector(self, scope, since, until):
        self.calls.append((scope, since, until))
        return {"scope": scope, "since_ms": since, "until_ms": until, "membership_count": 1,
                "rooms": [{"room_id": "!same", "messages": [], "errors": [], "coverage": "reached_cutoff"}]}

    def run_pass(self, collect=None, enumerate_rooms=None):
        return collection_pass.collect_pass(self.directory, ["prod", "dev"], 0, 100,
                                            enumerate_rooms or (lambda s: ["!same"]), collect or self.collector)

    def test_both_scopes_collected_before_consumer_admission(self):
        result = self.run_pass()
        self.assertTrue(result["consumer_admitted"])
        self.assertEqual(self.calls, [("prod", 0, 100), ("dev", 0, 100)])
        self.assertEqual(len(list((self.directory / "receipts").glob("*.json"))), 2)
        self.assertFalse(result["learning_progress_advanced"])

    def test_inventory_failure_never_admits_partial_scope_set(self):
        def enumerate_rooms(scope):
            if scope == "dev":
                raise TimeoutError()
            return ["!same"]
        result = self.run_pass(enumerate_rooms=enumerate_rooms)
        self.assertFalse(result["consumer_admitted"])
        self.assertEqual(result["errors"], {"dev": "TimeoutError"})
        self.assertIn("prod", result["receipts"])

    def test_failure_stage_distinguishes_identical_exception_types_without_payloads(self):
        secret = "fixture-secret-must-not-appear"
        def enumerate_rooms(scope):
            if scope == "dev":
                raise ValueError(secret)
            return ["!same"]
        result = self.run_pass(enumerate_rooms=enumerate_rooms)
        self.assertEqual(result["error_stages"], {"dev": "membership_read"})
        def collect(scope, since, until):
            if scope == "dev":
                raise ValueError(secret)
            return self.collector(scope, since, until)
        result = self.run_pass(collect=collect)
        self.assertEqual(result["error_stages"], {"dev": "history_read"})
        self.assertNotIn(secret, repr(result))
        def wrong_window(scope, since, until):
            value = self.collector(scope, since, until)
            if scope == "dev":
                value["until_ms"] += 1
            return value
        result = self.run_pass(collect=wrong_window)
        self.assertEqual(result["error_stages"], {"dev": "window_validation"})
        self.assertFalse(result["consumer_admitted"])

    def test_collector_cannot_drop_joined_rooms(self):
        result = self.run_pass(enumerate_rooms=lambda scope: ["!same", "!new"])
        self.assertFalse(result["consumer_admitted"])
        self.assertFalse((self.directory / "collection-state.json").exists())

    def test_page_budget_is_persisted_but_blocks_consumer(self):
        def collect(scope, since, until):
            value = self.collector(scope, since, until)
            if scope == "dev":
                value["rooms"][0]["coverage"] = "page_budget_exhausted"
            return value
        result = self.run_pass(collect=collect)
        self.assertFalse(result["consumer_admitted"])
        self.assertEqual(result["receipts"]["dev"]["pending_rooms"], ["!same"])
        self.assertEqual(len(list((self.directory / "receipts").glob("*.json"))), 2)

    def test_wrong_scope_or_window_never_publishes_receipt(self):
        for key, replacement in (("scope", "other"), ("since_ms", 50), ("until_ms", 200)):
            with self.subTest(key=key):
                def collect(scope, since, until):
                    value = self.collector(scope, since, until)
                    value[key] = replacement
                    return value
                result = self.run_pass(collect=collect)
                self.assertFalse(result["consumer_admitted"])
                self.assertFalse((self.directory / "collection-state.json").exists())

    def test_canonical_collector_real_cursor_and_scope_writer_round_trip(self):
        cursors = []
        def collect(scope, since, until):
            def fetch(room, before):
                cursors.append((scope, room, before))
                if before is None:
                    return {"messages": [{"event_id": "$new", "ts": 90}], "cursor": "server-cursor"}
                self.assertEqual(before, "server-cursor")
                return {"messages": [{"event_id": "$old", "ts": 0}], "cursor": None}
            value = collect_window(["!same"], fetch, since, until, pages=2)
            value["scope"] = scope
            return value
        result = self.run_pass(collect=collect)
        self.assertTrue(result["consumer_admitted"])
        self.assertEqual(cursors, [("prod", "!same", None), ("prod", "!same", "server-cursor"),
                                   ("dev", "!same", None), ("dev", "!same", "server-cursor")])

    def test_canonical_collector_timeout_keeps_other_scope_receipt(self):
        def collect(scope, since, until):
            def fetch(room, before):
                if scope == "dev":
                    raise TimeoutError()
                return {"messages": [], "cursor": None}
            value = collect_window(["!same"], fetch, since, until)
            value["scope"] = scope
            return value
        result = self.run_pass(collect=collect)
        self.assertFalse(result["consumer_admitted"])
        self.assertTrue(result["receipts"]["prod"]["complete_available_history"])
        self.assertEqual(result["receipts"]["dev"]["pending_rooms"], ["!same"])

    def test_retry_plan_preserves_failed_scope_bootstrap(self):
        def collect(scope, since, until):
            value = self.collector(scope, since, until)
            if scope == "dev":
                value["rooms"][0].update(coverage="read_failed", errors=["TimeoutError"])
            return value
        self.run_pass(collect=collect)
        self.calls.clear()
        result = self.run_pass()
        self.assertTrue(result["consumer_admitted"])
        self.assertEqual(self.calls, [("prod", 100, 100), ("dev", 0, 100)])

    def test_configured_scope_inventory_is_explicit(self):
        for scopes in ([], ["prod", "prod"], [None]):
            with self.assertRaises(ValueError):
                collection_pass.collect_pass(self.directory, scopes, 0, 100, lambda s: [], self.collector)


if __name__ == "__main__":
    unittest.main()
