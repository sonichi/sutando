#!/usr/bin/env python3
"""Unit tests for room_read — gate (default-deny + opt-in), backend selection,
normalisation, and graceful degrade (incl. 404/403 -> no-op). No network."""
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import room_read as rr  # noqa: E402

HS = "@agent.a:hs"
ROOM = "!roomA:hs"


def _clear_env(monkey_keys):
    for k in monkey_keys:
        os.environ.pop(k, None)


class GateTests(unittest.TestCase):
    def test_default_deny_empty_gate(self):
        self.assertFalse(rr.gate_allows(HS, ROOM, {}))

    def test_default_deny_unknown_agent(self):
        gate = {"@other:hs": {"rooms": [ROOM]}}
        self.assertFalse(rr.gate_allows(HS, ROOM, gate))

    def test_explicit_room_allow(self):
        gate = {HS: {"rooms": [ROOM]}}
        self.assertTrue(rr.gate_allows(HS, ROOM, gate))
        self.assertFalse(rr.gate_allows(HS, "!other:hs", gate))

    def test_all_member_rooms_grant(self):
        gate = {HS: {"all_member_rooms": True}}
        # member -> allow; unknown membership -> allow (backend enforces); non-member -> deny
        self.assertTrue(rr.gate_allows(HS, ROOM, gate, is_member=True))
        self.assertTrue(rr.gate_allows(HS, ROOM, gate, is_member=None))
        self.assertFalse(rr.gate_allows(HS, ROOM, gate, is_member=False))

    def test_malformed_entry_denies(self):
        self.assertFalse(rr.gate_allows(HS, ROOM, {HS: "yes"}))

    def test_load_gate_missing_file_is_empty(self):
        self.assertEqual(rr.load_gate("/nonexistent/path/gate.json"), {})


class BackendSelectTests(unittest.TestCase):
    KEYS = ["ROOM_READ_BACKEND", "AS_TOKEN", "APPSERVICE_TOKEN", "HOMESERVER",
            "HOMESERVER_URL", "RELAY_URL", "REMOTE_TASK_URL"]

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        _clear_env(self.KEYS)

    def tearDown(self):
        _clear_env(self.KEYS)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_explicit_wins(self):
        os.environ["ROOM_READ_BACKEND"] = "generic"
        os.environ["AS_TOKEN"] = "t"
        os.environ["HOMESERVER"] = "https://hs"
        self.assertEqual(rr._pick_backend(), "generic")

    def test_auto_appservice(self):
        os.environ["AS_TOKEN"] = "t"
        os.environ["HOMESERVER"] = "https://hs"
        self.assertEqual(rr._pick_backend(), "appservice")

    def test_auto_generic(self):
        os.environ["RELAY_URL"] = "https://relay"
        self.assertEqual(rr._pick_backend(), "generic")

    def test_auto_none(self):
        self.assertIsNone(rr._pick_backend())


class NormaliseTests(unittest.TestCase):
    def test_matrix_filters_non_messages(self):
        events = [
            {"type": "m.room.member", "sender": "@x:hs"},
            {"type": "m.room.message", "sender": "@a:hs",
             "origin_server_ts": 5, "content": {"body": "hi"}, "event_id": "$1"},
        ]
        out = rr._normalize_matrix(events)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0], {"sender": "@a:hs", "ts": 5, "body": "hi", "event_id": "$1"})

    def test_generic_field_fallbacks(self):
        items = [{"user_id": "@a:hs", "timestamp": 9, "text": "yo", "id": "x"}]
        out = rr._normalize_generic(items)
        self.assertEqual(out[0], {"sender": "@a:hs", "ts": 9, "body": "yo", "event_id": "x"})


class DegradeTests(unittest.TestCase):
    KEYS = ["AS_TOKEN", "APPSERVICE_TOKEN", "HOMESERVER", "HOMESERVER_URL",
            "RELAY_URL", "REMOTE_TASK_URL", "RELAY_TOKEN", "REMOTE_TASK_TOKEN"]

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        _clear_env(self.KEYS)

    def tearDown(self):
        _clear_env(self.KEYS)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_gate_deny_short_circuits(self):
        res = rr.read_room(ROOM, HS, gate={})
        self.assertFalse(res["ok"])
        self.assertIn("gate denied", res["reason"])

    def test_no_backend_configured(self):
        res = rr.read_room(ROOM, HS, gate={HS: {"rooms": [ROOM]}})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "no backend configured")

    def test_no_room_id(self):
        self.assertFalse(rr.read_room("", HS)["ok"])

    def test_generic_404_degrades(self):
        os.environ["RELAY_URL"] = "https://relay"
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with mock.patch.object(rr, "_http_get_json", side_effect=err):
            res = rr.read_room(ROOM, HS, gate={HS: {"rooms": [ROOM]}}, backend="generic")
        self.assertFalse(res["ok"])
        self.assertIn("unimplemented", res["reason"])
        self.assertEqual(res["messages"], [])

    def test_appservice_403_degrades(self):
        os.environ["AS_TOKEN"] = "t"
        os.environ["HOMESERVER"] = "https://hs"
        err = urllib.error.HTTPError("u", 403, "forbidden", {}, None)
        with mock.patch.object(rr, "_http_get_json", side_effect=err):
            res = rr.read_room(ROOM, HS, gate={HS: {"rooms": [ROOM]}}, backend="appservice")
        self.assertFalse(res["ok"])
        self.assertIn("not a member", res["reason"])

    def test_appservice_success_parses(self):
        os.environ["AS_TOKEN"] = "t"
        os.environ["HOMESERVER"] = "https://hs"
        body = {"chunk": [{"type": "m.room.message", "sender": "@a:hs",
                           "origin_server_ts": 1, "content": {"body": "ctx"}, "event_id": "$e"}]}
        with mock.patch.object(rr, "_http_get_json", return_value=(200, body)):
            res = rr.read_room(ROOM, HS, gate={HS: {"all_member_rooms": True}}, backend="appservice")
        self.assertTrue(res["ok"])
        self.assertEqual(res["messages"][0]["body"], "ctx")
        self.assertEqual(res["backend"], "appservice")

    def test_generic_success_parses(self):
        os.environ["RELAY_URL"] = "https://relay"
        body = {"messages": [{"sender": "@a:hs", "ts": 2, "body": "g"}]}
        with mock.patch.object(rr, "_http_get_json", return_value=(200, body)):
            res = rr.read_room(ROOM, HS, gate={HS: {"rooms": [ROOM]}}, backend="generic")
        self.assertTrue(res["ok"])
        self.assertEqual(res["messages"][0]["body"], "g")


if __name__ == "__main__":
    unittest.main(verbosity=2)
