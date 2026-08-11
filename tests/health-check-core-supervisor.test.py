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
            for st in ("crashed", "hung"):
                r = self._run(td, json.dumps({"state": st}))
                self.assertEqual(r["status"], "warn", st)
                self.assertIn("degraded", r["detail"])

    def test_gateway_down_warns_WHEN_THE_GATEWAY_IS_CONFIGURED(self):
        """The half that must keep warning: a configured gateway that is not running
        really does mean undelivered mobile messages."""
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(hc, "_gateway_configured", return_value=True):
                r = self._run(td, json.dumps({"state": "gateway-down"}))
        self.assertEqual(r["status"], "warn")
        self.assertIn("degraded", r["detail"])

    def test_gateway_down_is_OK_when_no_gateway_is_configured(self):
        """A Sutando-only host never launches the bridge — startup.sh is
        "deliberately silent when unconfigured" — so its absence is the designed
        state, not a degradation. Warning here gave every such install a permanent
        warn it could not clear, and check_gateway_bridge() already returns None
        for exactly this case; the two probes disagreed."""
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(hc, "_gateway_configured", return_value=False):
                r = self._run(td, json.dumps({"state": "gateway-down"}))
        self.assertEqual(r["status"], "ok")
        self.assertNotIn("degraded", r["detail"])
        self.assertIn("not configured", r["detail"])

    def test_healthy_states_ok(self):
        with tempfile.TemporaryDirectory() as td:
            for st in ("running", "idle-ready", "blocked-known"):
                self.assertEqual(self._run(td, json.dumps({"state": st}))["status"], "ok", st)

    def test_default_restart_never_sets_a_model_env(self):
        """No runtime gets a model pin: recovery must not change the core's model."""
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            script = repo / "src" / "agent" / "start-cli.sh"
            script.parent.mkdir(parents=True)
            script.write_text("#!/bin/bash\n")

            for runtime in ("claude", "codex"):
                captured = {}

                def run(*args, **kwargs):
                    captured.update(kwargs["env"])
                    return mock.Mock(returncode=0)

                with mock.patch.object(hc, "REPO_DIR", repo), \
                     mock.patch.object(hc, "_resolve_launch_env", return_value={"PATH": "/bin"}), \
                     mock.patch.object(hc, "resolve_core_runtime", return_value=runtime), \
                     mock.patch.object(hc.subprocess, "run", side_effect=run):
                    self.assertTrue(hc._default_core_restart())
                self.assertNotIn("SUTANDO_CORE_MODEL", captured,
                                 f"{runtime}: recovery restart must not pin a model")


if __name__ == "__main__":
    unittest.main()
