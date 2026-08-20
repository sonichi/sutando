#!/usr/bin/env python3
"""Tests for src/core_restart_intent.py — the easy-restart intent hand-off
(sonichi#2401): owner chat command parsing (exact-match, no prose triggers),
atomic write, consume-before-act semantics, and the stale/malformed drops
that keep an ancient or corrupt intent from firing a surprise restart.

Run: python3 tests/core-restart-intent.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))  # sibling workspace_default import
_SRC = os.path.join(_HERE, "..", "src", "core_restart_intent.py")
_spec = importlib.util.spec_from_file_location("core_restart_intent", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestParseRestartCommand(unittest.TestCase):
    def test_commands_match(self):
        for text, action in [("restart core", "restart"), ("Restart Core", "restart"),
                             ("  restart the core  ", "restart"), ("core restart", "restart"),
                             ("restart core!", "restart"), ("stop core", "stop"),
                             ("Stop the core.", "stop"), ("core stop", "stop")]:
            self.assertEqual(_mod.parse_restart_command(text), action, text)

    def test_prose_never_triggers(self):
        for text in ["we should restart core tomorrow", "the restart core question",
                     "restart", "core", "restart the core when convenient",
                     "please restart core now", "", None]:
            self.assertIsNone(_mod.parse_restart_command(text), repr(text))


class TestWriteConsume(unittest.TestCase):
    def test_roundtrip_restart(self):
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.write_intent(ws, "restart", "test")
            self.assertTrue(os.path.exists(p))
            self.assertEqual(_mod.consume_intent(ws), "restart")
            self.assertFalse(os.path.exists(p))  # consumed = deleted

    def test_consume_is_once(self):
        with tempfile.TemporaryDirectory() as ws:
            _mod.write_intent(ws, "stop", "test")
            self.assertEqual(_mod.consume_intent(ws), "stop")
            self.assertIsNone(_mod.consume_intent(ws))  # replay impossible

    def test_no_file_is_none(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertIsNone(_mod.consume_intent(ws))

    def test_unknown_action_rejected_at_write(self):
        with tempfile.TemporaryDirectory() as ws:
            with self.assertRaises(ValueError):
                _mod.write_intent(ws, "reboot-the-universe", "test")

    def test_stale_intent_dropped_and_consumed(self):
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.write_intent(ws, "restart", "test")
            self.assertIsNone(_mod.consume_intent(ws, now=time.time() + _mod.STALE_SEC + 1))
            self.assertFalse(os.path.exists(p))  # stale file still consumed

    def test_malformed_json_dropped_and_consumed(self):
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.intent_path(ws)
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as f:
                f.write("{not json")
            self.assertIsNone(_mod.consume_intent(ws))
            self.assertFalse(os.path.exists(p))

    def test_unknown_action_in_file_dropped(self):
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.intent_path(ws)
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as f:
                json.dump({"action": "explode", "requested_at": time.time()}, f)
            self.assertIsNone(_mod.consume_intent(ws))

    def test_non_dict_payload_dropped(self):
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.intent_path(ws)
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as f:
                json.dump(["restart"], f)
            self.assertIsNone(_mod.consume_intent(ws))

    def test_unlink_failure_fails_closed(self):
        # qingyun #2408 P1: if the delete fails, the file survives and the
        # next 5s poll would replay the same action — a restart LOOP. No
        # positive consume → NO action, ever.
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.write_intent(ws, "restart", "test")
            orig = os.unlink
            os.unlink = lambda pth: (_ for _ in ()).throw(OSError("locked"))
            try:
                self.assertIsNone(_mod.consume_intent(ws))   # fail closed
                self.assertIsNone(_mod.consume_intent(ws))   # and stays closed
            finally:
                os.unlink = orig
            self.assertTrue(os.path.exists(p))  # file intact for manual removal
            # once deletable again, the (non-stale) intent acts exactly once
            self.assertEqual(_mod.consume_intent(ws), "restart")
            self.assertIsNone(_mod.consume_intent(ws))

    def test_missing_requested_at_is_stale(self):
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.intent_path(ws)
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as f:
                json.dump({"action": "restart"}, f)  # requested_at absent → epoch 0 → stale
            self.assertIsNone(_mod.consume_intent(ws))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAwaitConsumption(unittest.TestCase):
    """Ack-on-consumption (#3183): the bridge must not promise a restart that
    no executor will perform. Consumption is proven by the file disappearing —
    not by probing for one named consumer implementation."""

    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def test_returns_true_when_executor_consumes(self):
        _mod.write_intent(self.ws, "restart", "test")
        calls = {"n": 0}

        def fake_sleep(_):
            calls["n"] += 1
            if calls["n"] == 2:  # an executor claims it on the 2nd poll
                _mod.consume_intent(self.ws)

        self.assertTrue(_mod.await_consumption(
            self.ws, timeout_sec=10, poll_sec=0, sleep=fake_sleep,
            now=lambda: 0.0))

    def test_returns_false_when_nothing_consumes(self):
        _mod.write_intent(self.ws, "restart", "test")
        t = {"v": 0.0}

        def fake_sleep(_):
            t["v"] += 1.0

        # File is never consumed -> must time out False, not hang or lie.
        self.assertFalse(_mod.await_consumption(
            self.ws, timeout_sec=3, poll_sec=0, sleep=fake_sleep,
            now=lambda: t["v"]))
        # ...and the intent is still on disk for a late executor / expiry.
        self.assertTrue(os.path.exists(_mod.intent_path(self.ws)))

    def test_already_consumed_before_first_poll_returns_immediately(self):
        # No intent on disk at all: return True without ever sleeping.
        def boom(_):
            raise AssertionError("must not sleep when already consumed")

        self.assertTrue(_mod.await_consumption(
            self.ws, timeout_sec=5, poll_sec=0, sleep=boom, now=lambda: 0.0))
