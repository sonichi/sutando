#!/usr/bin/env python3
"""A degraded secret scanner is invisible: startup.sh names it once at boot and
vault_intercept names it only when a `vault set` is refused."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
sys.modules["hc"] = hc
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass


def _run(interp_map, import_rc):
    """Probe with _bridge_interpreter and the detect_secrets import both stubbed."""
    def fake_interp(name):
        return interp_map.get(name)

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, import_rc.get(argv[0], 0))

    with mock.patch.object(hc, "_bridge_interpreter", fake_interp), \
         mock.patch.object(hc.subprocess, "run", fake_run):
        return hc.check_secret_scanner_mode()


ALL_THREE = {"telegram-bridge": "/py/a", "discord-bridge": "/py/b", "slack-bridge": "/py/c"}


class SecretScannerModeIsStandingStatus(unittest.TestCase):
    def test_all_interpreters_have_it(self):
        r = _run(ALL_THREE, {})
        self.assertEqual(r["status"], "ok")
        self.assertIn("3 bridge interpreter", r["detail"])

    def test_one_degraded_interpreter_warns_and_names_it(self):
        r = _run(ALL_THREE, {"/py/b": 1})
        self.assertEqual(r["status"], "warn")
        self.assertIn("/py/b", r["detail"])
        self.assertIn("1/3", r["detail"])

    def test_fix_hint_names_the_degraded_interpreter_not_bare_python3(self):
        # startup.sh's lesson: the fix must name THIS interpreter, because the
        # bridges rarely run the python3 first on PATH.
        r = _run(ALL_THREE, {"/py/c": 1})
        self.assertIn("/py/c -m pip install detect-secrets", r["detail"])

    def test_duplicate_interpreters_are_probed_once(self):
        r = _run({k: "/py/same" for k in ALL_THREE}, {})
        self.assertIn("1 bridge interpreter", r["detail"])

    def test_unlaunchable_bridges_are_skipped_not_counted_degraded(self):
        # _bridge_interpreter returns None when no candidate can import the
        # bridge's client lib; that bridge's own probe owns the failure.
        r = _run({"telegram-bridge": "/py/a", "discord-bridge": None,
                  "slack-bridge": None}, {})
        self.assertEqual(r["status"], "ok")
        self.assertIn("1 bridge interpreter", r["detail"])

    def test_no_launchable_interpreter_is_unknown_not_healthy(self):
        r = _run({k: None for k in ALL_THREE}, {})
        self.assertEqual(r["status"], "warn")
        self.assertIn("unknown", r["detail"])

    def test_a_failed_probe_is_unknown_not_healthy(self):
        # An OSError/timeout must never read as "detect-secrets is present".
        def boom(argv, **kw):
            raise OSError("no such interpreter")
        with mock.patch.object(hc, "_bridge_interpreter", lambda n: ALL_THREE.get(n)), \
             mock.patch.object(hc.subprocess, "run", boom):
            r = hc.check_secret_scanner_mode()
        self.assertEqual(r["status"], "warn")
        self.assertIn("not proven healthy", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
