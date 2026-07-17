#!/usr/bin/env python3
"""Tests for health-check.py's check_core_supervisor — the OSS surface for the
core-supervisor (Agent Shepherd M1) signal. The desktop app renders the signal
as a banner; OSS users see it here in `python3 src/health-check.py`.

Covers: file missing → ok · malformed → ok · blocked-human/logged-out → warn
(needs you) · crashed/hung/gateway-down → warn (degraded) · running/idle-ready/
blocked-known → ok.

Run: python3 tests/health-check-core-supervisor.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src", "health-check.py")
_spec = importlib.util.spec_from_file_location("health_check", _SRC)
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)


class TestCheckCoreSupervisor(unittest.TestCase):
    def _run(self, td, payload):
        sig = os.path.join(td, "state", "core-supervisor.json")
        os.makedirs(os.path.dirname(sig), exist_ok=True)
        if payload is not None:
            with open(sig, "w") as f:
                f.write(payload)

        def _rp(name, ws):
            if name == "core-supervisor.json":
                return pathlib.Path(sig)
            return pathlib.Path(td) / name

        with mock.patch.object(hc, "status_read_path", side_effect=_rp):
            return hc.check_core_supervisor()

    def test_missing_file_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run(td, None)
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["name"], "core-supervisor")

    def test_malformed_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._run(td, "{not json")["status"], "ok")

    def test_blocked_human_warns_needs_you_with_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run(td, json.dumps({"state": "blocked-human",
                                          "prompt": "Login\nSelect login method"}))
            self.assertEqual(r["status"], "warn")
            self.assertIn("needs you", r["detail"])
            self.assertIn("Login", r["detail"])          # first prompt line only
            self.assertNotIn("Select", r["detail"])

    def test_logged_out_warns_needs_you(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._run(td, json.dumps({"state": "logged-out"}))["status"], "warn")

    def test_degraded_states_warn(self):
        with tempfile.TemporaryDirectory() as td:
            for st in ("crashed", "hung", "gateway-down"):
                r = self._run(td, json.dumps({"state": st}))
                self.assertEqual(r["status"], "warn", st)
                self.assertIn("degraded", r["detail"])

    def test_healthy_states_ok(self):
        with tempfile.TemporaryDirectory() as td:
            for st in ("running", "idle-ready", "blocked-known"):
                self.assertEqual(self._run(td, json.dumps({"state": st}))["status"], "ok", st)


if __name__ == "__main__":
    unittest.main()
