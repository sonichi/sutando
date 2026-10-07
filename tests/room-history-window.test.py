#!/usr/bin/env python3
"""Offline incident replays and boundary tests. No external writes or real network."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/agent-room-ops"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


from history_collection import collect_window
cli = load("room_cli_test", "skills/agent-room-ops/room_ops.py")

def event(n, ts=None):
    return {"event_id": str(n), "ts": n if ts is None else ts, "body": "fixture"}


class HistoryTests(unittest.TestCase):
    def test_wrapper_preserves_scope_and_redacts_without_mutations(self):
        module = cli._history
        with patch.object(module, "joined_rooms", return_value={"ok": True, "rooms": ["room"]}), patch.object(module, "gateway", return_value=("https://scope.test", {})), patch.object(module, "load_gate", return_value={}), patch.object(module, "gate_allows", return_value=True), patch.object(module, "_redactor", return_value=lambda text: "[redacted]"), patch.object(module, "http_request", return_value=(200, json.dumps({"messages": [event(15)]}), {})) as transport:
            got = module.history(10, 20)
        self.assertEqual(got["scope"], "scope.test")
        self.assertEqual(got["rooms"][0]["messages"][0]["body"], "[redacted]")
        self.assertEqual(transport.call_args.args[0], "GET")

    def test_all_77_memberships_including_never_addressed_room(self):
        rooms = [str(i) for i in range(77)]
        got = collect_window(rooms, lambda room, before: {"messages": [event(15)], "cursor": None}, 10, 20)
        self.assertEqual(got["membership_count"], 77)
        self.assertEqual(got["window_messages"], 77)
        self.assertTrue(got["complete_available_history"])

    def test_more_than_60_messages_real_cursor_overlap(self):
        calls = []
        def fetch(room, cursor):
            calls.append(cursor)
            return {"messages": [event(i) for i in range(200, 99, -1)], "cursor": "next"} if cursor is None else {"messages": [event(i) for i in range(100, 0, -1)], "cursor": None}
        got = collect_window(["r"], fetch, 50, 200)
        self.assertEqual(calls, [None, "next"])
        self.assertEqual(got["window_messages"], 151)

    def test_events_outside_window_excluded(self):
        got = collect_window(["r"], lambda *args: {"messages": [event(1), event(15), event(25)]}, 10, 20)
        self.assertEqual([m["event_id"] for m in got["rooms"][0]["messages"]], ["15"])

    def test_cutoff_timestamp_ties_continue_to_next_page(self):
        calls = []

        def fetch(room, cursor):
            calls.append(cursor)
            if cursor is None:
                return {"messages": [event("first", 10)], "cursor": "next"}
            return {"messages": [event("second", 10), event("older", 9)], "cursor": "unused"}

        got = collect_window(["r"], fetch, 10, 20)
        self.assertTrue(got["ok"])
        self.assertEqual(calls, [None, "next"])
        self.assertEqual([m["event_id"] for m in got["rooms"][0]["messages"]], ["first", "second"])

    def test_cutoff_timestamp_ties_at_budget_are_incomplete(self):
        got = collect_window(["r"], lambda *args: {"messages": [event("edge", 10)], "cursor": "next"}, 10, 20, pages=1)
        self.assertFalse(got["ok"])
        self.assertEqual(got["rooms"][0]["coverage"], "page_budget_exhausted")

    def test_declined_page_is_not_end_of_history(self):
        for denial in ({"ok": False}, {"error": "private detail"}):
            page = {"messages": [], "cursor": None, **denial}
            got = collect_window(["r"], lambda *args: page, 10, 20)
            self.assertFalse(got["ok"])
            self.assertFalse(got["rooms"][0]["covered_available_history"])
            self.assertNotIn("private detail", json.dumps(got))

    def test_timeout_is_unknown_not_quiet(self):
        def fetch(*args):
            raise TimeoutError("private detail")
        got = collect_window(["r"], fetch, 10, 20)
        self.assertFalse(got["ok"])
        self.assertEqual(got["rooms"][0]["coverage"], "read_failed")
        self.assertNotIn("private detail", json.dumps(got))

    def test_cursor_cycle_not_marked_complete(self):
        got = collect_window(["r"], lambda *args: {"messages": [event(15)], "cursor": "same"}, 10, 20)
        self.assertFalse(got["ok"])
        self.assertEqual(got["rooms"][0]["pages"], 2)

    def test_budget_exhaustion_exposes_truncation(self):
        got = collect_window(["r"], lambda *args: {"messages": [event(15)], "cursor": "next"}, 10, 20, pages=1)
        self.assertEqual(got["rooms"][0]["coverage"], "page_budget_exhausted")
        self.assertFalse(got["complete_available_history"])

    def test_malformed_timestamp_cannot_advance_complete_coverage(self):
        for ts in (None, True, float("nan")):
            got = collect_window(["r"], lambda *args: {"messages": [{"event_id": "x", "ts": ts}]}, 10, 20)
            self.assertFalse(got["ok"])

    def test_malformed_page_cannot_be_empty_success(self):
        self.assertFalse(collect_window(["r"], lambda *args: {"error": "oops"}, 10, 20)["ok"])

    def test_new_membership_is_enumerated_each_collection(self):
        fetch = lambda *args: {"messages": []}
        self.assertEqual(collect_window(["old"], fetch, 10, 20)["membership_count"], 1)
        self.assertEqual(collect_window(["old", "new"], fetch, 10, 20)["membership_count"], 2)

    def test_invalid_windows_and_inventory_refused(self):
        for ids, start, end, pages in ((["r"], 20, 10, 1), (["r"], 10, 20, 0), (None, 10, 20, 1)):
            with self.assertRaises(ValueError):
                collect_window(ids, lambda *args: {}, start, end, pages)

    def test_body_file_preserves_multiline_literal_message(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "body.txt"
            body = "Literal `code` $(do not execute)\nSecond line"
            file.write_text(body)
            with patch.object(cli._say, "say", return_value={"ok": True, "event_id": "event"}) as say, patch.object(cli, "_record_say"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli._main(["say", "room", "--body-file", str(file)]), 0)
            self.assertEqual(say.call_args.args[0], body)

    def test_invalid_or_ambiguous_body_file_never_sends(self):
        for args in (["say", "room"], ["say", "room", "text", "--body-file", "missing"], ["say", "room", "--body-file", "missing"]):
            with patch.object(cli._say, "say") as say, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli._main(args), 2)
                say.assert_not_called()

    def test_naive_history_timestamp_refused_before_network(self):
        with patch.object(cli._history, "history") as collect, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli._main(["--strict", "history", "--since", "2026-10-05T04:00:00"]), 1)
            collect.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
