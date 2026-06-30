#!/usr/bin/env python3
"""Unit tests for room_read — optional client gate, the single relay-verb
backend, normalisation, and graceful degrade (incl. 404/403 -> no-op). No
network."""
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import room_read as rr  # noqa: E402

HS = "@agent.a:hs"
ROOM = "!roomA:hs"


def _clear_env(keys):
    for k in keys:
        os.environ.pop(k, None)


class GateTests(unittest.TestCase):
    def test_no_gate_defers_to_relay(self):
        # gate is None -> no client pre-filter (relay enforces membership)
        self.assertTrue(rr.gate_allows(HS, ROOM, None))

    def test_empty_gate_denies_all(self):
        self.assertFalse(rr.gate_allows(HS, ROOM, {}))

    def test_unknown_agent_denied(self):
        self.assertFalse(rr.gate_allows(HS, ROOM, {"@other:hs": {"rooms": [ROOM]}}))

    def test_explicit_room_allow(self):
        gate = {HS: {"rooms": [ROOM]}}
        self.assertTrue(rr.gate_allows(HS, ROOM, gate))
        self.assertFalse(rr.gate_allows(HS, "!other:hs", gate))

    def test_all_member_rooms_grant(self):
        gate = {HS: {"all_member_rooms": True}}
        self.assertTrue(rr.gate_allows(HS, ROOM, gate, is_member=True))
        self.assertTrue(rr.gate_allows(HS, ROOM, gate, is_member=None))
        self.assertFalse(rr.gate_allows(HS, ROOM, gate, is_member=False))

    def test_malformed_entry_denies(self):
        self.assertFalse(rr.gate_allows(HS, ROOM, {HS: "yes"}))

    def test_load_gate_missing_file_is_none(self):
        # missing file -> None (defer to relay), distinct from empty {} (deny-all)
        self.assertIsNone(rr.load_gate("/nonexistent/path/gate.json"))


class NormaliseTests(unittest.TestCase):
    def test_field_fallbacks(self):
        items = [{"user_id": "@a:hs", "timestamp": 9, "text": "yo", "id": "x"}]
        self.assertEqual(rr._normalize(items)[0],
                         {"sender": "@a:hs", "ts": 9, "body": "yo", "event_id": "x"})

    def test_primary_fields(self):
        items = [{"sender": "@a:hs", "ts": 1, "body": "hi", "event_id": "$e"}]
        self.assertEqual(rr._normalize(items)[0],
                         {"sender": "@a:hs", "ts": 1, "body": "hi", "event_id": "$e"})


class ReadTests(unittest.TestCase):
    KEYS = ["RELAY_URL", "REMOTE_TASK_URL", "RELAY_TOKEN", "REMOTE_TASK_TOKEN"]

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        _clear_env(self.KEYS)

    def tearDown(self):
        _clear_env(self.KEYS)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_gate_deny_short_circuits(self):
        res = rr.read_room(ROOM, HS, gate={})  # empty gate -> deny
        self.assertFalse(res["ok"])
        self.assertIn("client gate denied", res["reason"])

    def test_no_relay_configured(self):
        res = rr.read_room(ROOM, HS, gate={HS: {"rooms": [ROOM]}})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "no RELAY_URL configured")

    def test_no_room_id(self):
        self.assertFalse(rr.read_room("", HS)["ok"])

    def test_404_degrades(self):
        os.environ["RELAY_URL"] = "https://relay"
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with mock.patch.object(rr, "_http_get_json", side_effect=err):
            res = rr.read_room(ROOM, HS, gate={HS: {"rooms": [ROOM]}})
        self.assertFalse(res["ok"])
        self.assertIn("unimplemented", res["reason"])
        self.assertEqual(res["messages"], [])

    def test_403_non_member_degrades(self):
        os.environ["RELAY_URL"] = "https://relay"
        err = urllib.error.HTTPError("u", 403, "forbidden", {}, None)
        with mock.patch.object(rr, "_http_get_json", side_effect=err):
            res = rr.read_room(ROOM, HS, gate={HS: {"rooms": [ROOM]}})
        self.assertFalse(res["ok"])
        self.assertIn("not a joined member", res["reason"])

    def test_network_error_degrades(self):
        os.environ["RELAY_URL"] = "https://relay"
        with mock.patch.object(rr, "_http_get_json", side_effect=urllib.error.URLError("down")):
            res = rr.read_room(ROOM, HS, gate={HS: {"rooms": [ROOM]}})
        self.assertFalse(res["ok"])
        self.assertIn("network", res["reason"])

    def test_success_parses(self):
        os.environ["RELAY_URL"] = "https://relay"
        body = {"messages": [{"sender": "@a:hs", "ts": 2, "body": "g", "event_id": "$1"}]}
        with mock.patch.object(rr, "_http_get_json", return_value=(200, body)):
            res = rr.read_room(ROOM, HS, gate={HS: {"rooms": [ROOM]}})
        self.assertTrue(res["ok"])
        self.assertEqual(res["messages"][0]["body"], "g")

    def test_no_gate_file_still_reads(self):
        # gate=None (no file) -> client defers; relay call still happens
        os.environ["RELAY_URL"] = "https://relay"
        body = {"messages": [{"sender": "@a:hs", "ts": 2, "body": "ctx"}]}
        with mock.patch.object(rr, "_http_get_json", return_value=(200, body)):
            res = rr.read_room(ROOM, HS, gate=None)
        self.assertTrue(res["ok"])

    def test_before_param_passed(self):
        os.environ["RELAY_URL"] = "https://relay"
        captured = {}

        def fake(url, headers=None):
            captured["url"] = url
            return 200, {"messages": []}

        with mock.patch.object(rr, "_http_get_json", side_effect=fake):
            rr.read_room(ROOM, HS, gate={HS: {"rooms": [ROOM]}}, before="$tok")
        self.assertIn("before=%24tok", captured["url"])


class ClampAndExitTests(unittest.TestCase):
    KEYS = ["RELAY_URL", "REMOTE_TASK_URL", "RELAY_TOKEN", "REMOTE_TASK_TOKEN"]

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        _clear_env(self.KEYS)
        os.environ["RELAY_URL"] = "https://relay"

    def tearDown(self):
        _clear_env(self.KEYS)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_limit_clamped_to_max(self):
        captured = {}

        def fake(url, headers=None):
            captured["url"] = url
            return 200, {"messages": []}

        with mock.patch.object(rr, "_http_get_json", side_effect=fake):
            rr.read_room(ROOM, HS, limit=9999, gate={HS: {"rooms": [ROOM]}})
        self.assertIn(f"limit={rr.MAX_LIMIT}", captured["url"])

    def test_limit_floor_is_one(self):
        captured = {}

        def fake(url, headers=None):
            captured["url"] = url
            return 200, {"messages": []}

        with mock.patch.object(rr, "_http_get_json", side_effect=fake):
            rr.read_room(ROOM, HS, limit=0, gate={HS: {"rooms": [ROOM]}})
        self.assertIn("limit=1", captured["url"])

    def test_main_exits_zero_on_no_context(self):
        # gate-deny -> ok:false, but CLI should still exit 0 (graceful, not a failed task)
        with mock.patch.object(rr, "load_gate", return_value={}):
            rc = rr._main([ROOM, "--agent", HS])
        self.assertEqual(rc, 0)

    def test_main_exits_zero_on_success(self):
        with mock.patch.object(rr, "load_gate", return_value={HS: {"rooms": [ROOM]}}), \
             mock.patch.object(rr, "_http_get_json", return_value=(200, {"messages": []})):
            rc = rr._main([ROOM, "--agent", HS])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
