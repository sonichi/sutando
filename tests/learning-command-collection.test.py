#!/usr/bin/env python3
import json
import datetime
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/learning-window/scripts"))
from command_collection import collect_commands


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state"
        self.calls = []

    def runner(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        scope = argv[0]
        if argv[2] == "rooms":
            value = {"ok": True, "rooms": ["!room"]}
        else:
            value = {"ok": True, "scope": scope, "membership_count": 1, "since_ms": 0, "until_ms": 100,
                     "rooms": [{"room_id": "!room", "messages": [], "errors": [], "coverage": "server_no_cursor"}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    def call(self, runner=None):
        return collect_commands(self.path, {"prod": ["prod"], "dev": ["dev"]}, 0, 100, runner=runner or self.runner)

    def test_strict_read_only_verbs_and_timeout_delegation(self):
        self.assertTrue(self.call()["consumer_admitted"])
        self.assertEqual([a[0][:3] for a in self.calls], [["prod", "--strict", "rooms"], ["dev", "--strict", "rooms"],
                                                        ["prod", "--strict", "history"], ["dev", "--strict", "history"]])
        for argv, kwargs in self.calls:
            self.assertEqual(kwargs, {"capture_output": True, "text": True, "timeout": 600})
            self.assertNotIn("say", argv)

    def test_partial_strict_history_failure_persists_unread_evidence(self):
        def runner(argv, **kwargs):
            result = self.runner(argv, **kwargs)
            if argv[0] == "dev" and argv[2] == "history":
                value = json.loads(result.stdout)
                value["ok"] = False
                value["rooms"][0].update(coverage="read_failed", errors=["TimeoutError"])
                return SimpleNamespace(returncode=1, stdout=json.dumps(value))
            return result
        result = self.call(runner)
        self.assertFalse(result["consumer_admitted"])
        self.assertEqual(result["receipts"]["dev"]["pending_rooms"], ["!room"])
        self.assertEqual(len(list((self.path / "receipts").glob("*.json"))), 2)

    def test_timeout_never_admits_consumer(self):
        def runner(argv, **kwargs):
            if argv[0] == "dev":
                raise subprocess.TimeoutExpired(argv, 600)
            return self.runner(argv, **kwargs)
        result = self.call(runner)
        self.assertFalse(result["consumer_admitted"])
        self.assertEqual(result["errors"]["dev"], "TimeoutExpired")

    def test_wrong_gateway_scope_cannot_advance(self):
        def runner(argv, **kwargs):
            result = self.runner(argv, **kwargs)
            if argv[2] == "history":
                value = json.loads(result.stdout)
                value["scope"] = "wrong-host"
                result.stdout = json.dumps(value)
            return result
        self.assertFalse(self.call(runner)["consumer_admitted"])
        self.assertFalse((self.path / "collection-state.json").exists())

    def test_malformed_or_contradictory_output_does_not_advance(self):
        for rc, output in [(1, '{}'), (1, '{"ok":true}'), (0, '{"ok":false}'), (0, 'not-json')]:
            result = self.call(lambda argv, **kwargs: SimpleNamespace(returncode=rc, stdout=output))
            self.assertFalse(result["consumer_admitted"])
            self.assertFalse((self.path / "collection-state.json").exists())

    def test_invalid_configuration_never_invokes_command(self):
        for capabilities in ({}, {"prod": "shell string"}, {"prod": []}):
            with self.assertRaises(ValueError):
                collect_commands(self.path, capabilities, 0, 100, runner=self.runner)
        self.assertEqual(self.calls, [])

    def test_live_fractional_timestamp_roundtrip_admits_both_scopes(self):
        def runner(argv, **kwargs):
            if argv[2] == "rooms":
                value = {"ok": True, "rooms": ["!room"]}
            else:
                def parsed(flag):
                    return datetime.datetime.fromisoformat(argv[argv.index(flag) + 1]).timestamp() * 1000
                value = {"ok": True, "scope": argv[0], "membership_count": 1,
                         "since_ms": parsed("--since"), "until_ms": parsed("--until"),
                         "rooms": [{"room_id": "!room", "messages": [], "errors": [], "coverage": "server_no_cursor"}]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(value))
        result = collect_commands(self.path, {"prod": ["prod"], "dev": ["dev"]},
                                  1791178590546.104, 1791190832646.387, runner=runner)
        self.assertTrue(result["consumer_admitted"], result)
        self.assertEqual(len(result["receipts"]), 2)

    def test_material_window_difference_still_refuses(self):
        def runner(argv, **kwargs):
            result = self.runner(argv, **kwargs)
            if argv[2] == "history":
                value = json.loads(result.stdout)
                value["until_ms"] += 1
                result.stdout = json.dumps(value)
            return result
        self.assertFalse(self.call(runner)["consumer_admitted"])
        self.assertFalse((self.path / "collection-state.json").exists())


if __name__ == "__main__":
    unittest.main()
