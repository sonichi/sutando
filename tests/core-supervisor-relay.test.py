#!/usr/bin/env python3
"""Tests for src/core-supervisor-relay.py — the COMMUNICATOR (outbound ESCALATE).

Covers the pure decision (which states escalate), the debounce (a persistent
prompt fires once; a new prompt re-fires), message composition, and one full
--dry-run CLI cycle. Signal fixtures match the monitor's core-supervisor.json
schema exactly.

Run: python3 tests/core-supervisor-relay.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src", "core-supervisor-relay.py")
_spec = importlib.util.spec_from_file_location("core_supervisor_relay", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
should_escalate = _mod.should_escalate
compose_message = _mod.compose_message
run_cycle = _mod.run_cycle
main = _mod.main

_LOGIN = {"state": "blocked-human", "detail": "awaiting user: login",
          "prompt": "Login\nSelect login method:\n  1. Claude account", "kind": "login"}
_LOGGED_OUT = {"state": "logged-out", "detail": "core not authenticated (needs /login)",
               "prompt": None, "kind": None}
_IDLE = {"state": "idle-ready", "detail": "ready for a task", "prompt": None, "kind": None}
_RUNNING = {"state": "running", "detail": "actively processing", "prompt": None, "kind": None}
_CRASHED = {"state": "crashed", "detail": "core process/session not found", "prompt": None}
_HUNG = {"state": "hung", "detail": "core alive but stalled", "prompt": "…", "kind": "unknown"}


class TestShouldEscalate(unittest.TestCase):
    def test_login_escalates(self):
        esc, h = should_escalate(_LOGIN, None)
        self.assertTrue(esc)
        self.assertIsNotNone(h)

    def test_logged_out_escalates(self):
        self.assertTrue(should_escalate(_LOGGED_OUT, None)[0])

    def test_idle_and_running_never_escalate(self):
        self.assertFalse(should_escalate(_IDLE, None)[0])
        self.assertFalse(should_escalate(_RUNNING, None)[0])

    def test_crashed_and_hung_go_to_recover_not_user(self):
        # RECOVER (restart) handles these, not user-escalation → no notification.
        self.assertFalse(should_escalate(_CRASHED, None)[0])
        self.assertFalse(should_escalate(_HUNG, None)[0])

    def test_debounce_same_prompt_fires_once(self):
        esc1, h1 = should_escalate(_LOGIN, None)
        self.assertTrue(esc1)
        esc2, h2 = should_escalate(_LOGIN, h1)  # same prompt already escalated
        self.assertFalse(esc2)
        self.assertEqual(h1, h2)

    def test_new_prompt_reescalates(self):
        _, h1 = should_escalate(_LOGIN, None)
        other = {"state": "blocked-human", "detail": "awaiting user: permission",
                 "prompt": "Do you want to proceed?", "kind": "permission"}
        esc, h2 = should_escalate(other, h1)
        self.assertTrue(esc)
        self.assertNotEqual(h1, h2)

    def test_healthy_tick_preserves_last_hash(self):
        # A transient healthy tick between two identical blockers must NOT reset the
        # debounce (else the same login would double-notify).
        _, h1 = should_escalate(_LOGIN, None)
        _, h_mid = should_escalate(_RUNNING, h1)
        self.assertEqual(h_mid, h1)
        self.assertFalse(should_escalate(_LOGIN, h_mid)[0])


class TestComposeMessage(unittest.TestCase):
    def test_includes_detail_and_prompt_excerpt(self):
        m = compose_message(_LOGIN)
        self.assertIn("awaiting user: login", m)
        self.assertIn("Login", m)  # first prompt line
        self.assertIn("resolve", m)

    def test_handles_no_prompt(self):
        m = compose_message(_LOGGED_OUT)
        self.assertIn("not authenticated", m)
        self.assertTrue(m.endswith("resolve."))

    def test_truncates_long_prompt(self):
        big = {"state": "blocked-human", "detail": "awaiting user: unknown",
               "prompt": "x" * 500, "kind": "unknown"}
        m = compose_message(big)
        self.assertLess(len(m), 260)

    def test_kind_appended_when_not_in_detail(self):
        sig = {"state": "blocked-human", "detail": "the core is waiting",
               "prompt": "pick one", "kind": "selection"}
        self.assertIn("(selection)", compose_message(sig))


class TestRunCycleAndCli(unittest.TestCase):
    def test_dry_run_cycle_escalates_login(self):
        with tempfile.TemporaryDirectory() as td:
            sf = os.path.join(td, "relay.state")
            msg = run_cycle(_LOGIN, sf, macos=False, dry_run=True)
            self.assertIsNotNone(msg)
            # dry-run must NOT persist state (so a real run still fires).
            self.assertFalse(os.path.exists(sf))

    def test_real_cycle_persists_and_debounces(self):
        with tempfile.TemporaryDirectory() as td:
            sf = os.path.join(td, "state", "relay.state")
            first = run_cycle(_LOGIN, sf, macos=False)  # no channel → macOS suppressed, still decides
            self.assertIsNotNone(first)
            self.assertTrue(os.path.exists(sf))
            second = run_cycle(_LOGIN, sf, macos=False)  # same prompt → suppressed
            self.assertIsNone(second)

    def test_cli_dry_run_on_signal_file(self):
        with tempfile.TemporaryDirectory() as td:
            sig = os.path.join(td, "core-supervisor.json")
            with open(sig, "w") as f:
                json.dump(_LOGIN, f)
            rc = main(["--signal", sig, "--no-macos", "--dry-run"])
            self.assertEqual(rc, 0)

    def test_cli_missing_signal_degrades_quietly(self):
        rc = main(["--signal", "/nonexistent/core-supervisor.json", "--no-macos"])
        self.assertEqual(rc, 0)

    def test_cli_non_escalating_signal(self):
        with tempfile.TemporaryDirectory() as td:
            sig = os.path.join(td, "core-supervisor.json")
            with open(sig, "w") as f:
                json.dump(_IDLE, f)
            self.assertEqual(main(["--signal", sig, "--no-macos"]), 0)

    def test_cli_non_dict_signal_degrades(self):
        with tempfile.TemporaryDirectory() as td:
            sig = os.path.join(td, "core-supervisor.json")
            with open(sig, "w") as f:
                json.dump([1, 2, 3], f)  # valid JSON, wrong shape
            self.assertEqual(main(["--signal", sig, "--no-macos"]), 0)

    def test_cycle_without_state_file_still_emits(self):
        # No --state-file → no debounce persistence, but the escalation still fires.
        msg = run_cycle(_LOGIN, "", macos=False)
        self.assertIsNotNone(msg)

    def test_run_cycle_dispatches_to_both_surfaces(self):
        # Verify the dispatch call-sites (macOS + channel) fire without invoking the
        # real external I/O — the adapters themselves are best-effort side effects.
        calls = []
        orig_m, orig_c = _mod._macos_notify, _mod._channel_notify
        _mod._macos_notify = lambda m: calls.append(("macos", m))
        _mod._channel_notify = lambda m, s, c: calls.append(("chan", s, c))
        try:
            with tempfile.TemporaryDirectory() as td:
                run_cycle(_LOGIN, os.path.join(td, "s.state"),
                          macos=True, source="discord", channel="123")
        finally:
            _mod._macos_notify, _mod._channel_notify = orig_m, orig_c
        kinds = [c[0] for c in calls]
        self.assertIn("macos", kinds)
        self.assertIn("chan", kinds)


if __name__ == "__main__":
    unittest.main()
