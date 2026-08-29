#!/usr/bin/env python3
"""Non-ARMED pin verdicts must surface on normal service paths, not vanish.

evaluate() reports ORPHAN/MISMATCH/EXPIRED precisely so a lost pin becomes a
finding, but the credential-proxy adapter kept only armed_detail(): a healthy
replacement rendered plain `ok` and a down service plain `not running`, while
the stale pin sat silent forever. These controls drive the PUBLIC
check_credential_proxy() entry with only the socket/process seams replaced,
and write pins through the production writer.

Run: python3 tests/health-check-pin-orphan-surfaces.test.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-orphan-")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import process_pins  # noqa: E402

spec = importlib.util.spec_from_file_location("hc_orphan", REPO / "src/health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

LSTART = "Sat Aug 23 12:24:57 2026"
EXP = "2026-12-31T00:00:00Z"


class OrphanSurfacesOnServicePaths(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Path(tempfile.mkdtemp(prefix="ws-orphan-"))
        self._saved = (hc.WORKSPACE_DIR, hc.check_port,
                       hc._proc_lstarts, hc.mark_stale_if_outdated)
        hc.WORKSPACE_DIR = self.ws
        self.pin_file = self.ws / "state" / "process-pins.json"
        # Staleness is not under test; keep the seam inert.
        hc.mark_stale_if_outdated = lambda *a, **k: None

    def tearDown(self) -> None:
        (hc.WORKSPACE_DIR, hc.check_port,
         hc._proc_lstarts, hc.mark_stale_if_outdated) = self._saved

    def _port(self, status: str):
        hc.check_port = lambda *a, **k: {"name": "credential-proxy",
                                         "status": status, "detail": "port 7846"}

    def test_healthy_replacement_surfaces_orphan(self) -> None:
        # Pin names pid 111; the live process is a DIFFERENT pid — orphan.
        process_pins.arm_pin(self.pin_file, "credential-proxy", "111",
                             LSTART, "branch witness", EXP)
        self._port("ok")
        hc._proc_lstarts = lambda pat: ([0.0], {"222": LSTART})
        check = hc.check_credential_proxy()
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("no longer running", check["detail"], check)
        self.assertIn("port 7846", check["detail"], check)   # liveness kept
        self.assertTrue(check["live"], check)
        self.assertNotIn("restart_veto", check, check)

    def test_down_service_surfaces_orphan(self) -> None:
        process_pins.arm_pin(self.pin_file, "credential-proxy", "111",
                             LSTART, "branch witness", EXP)
        self._port("down")
        hc._proc_lstarts = lambda pat: ([], {})
        check = hc.check_credential_proxy()
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("not running (optional)", check["detail"], check)
        self.assertIn("no longer running", check["detail"], check)

    def test_armed_plus_orphan_composition_keeps_both(self) -> None:
        process_pins.arm_pin(self.pin_file, "credential-proxy", "222",
                             LSTART, "live witness", EXP)      # ARMED (pid live)
        process_pins.arm_pin(self.pin_file, "credential-proxy", "111",
                             LSTART, "old witness", EXP)       # ORPHAN (pid gone)
        self._port("ok")
        hc._proc_lstarts = lambda pat: ([0.0], {"222": LSTART})
        check = hc.check_credential_proxy()
        self.assertIn("DO NOT RESTART credential-proxy pid 222",
                      check.get("restart_veto", ""), check)
        self.assertIn("no longer running", check["detail"], check)
        self.assertEqual(check["status"], "warn", check)

    def test_CONTROL_no_pins_healthy_stays_plain_ok(self) -> None:
        self._port("ok")
        hc._proc_lstarts = lambda pat: ([0.0], {"222": LSTART})
        check = hc.check_credential_proxy()
        self.assertEqual(check["status"], "ok", check)
        self.assertEqual(check["detail"], "port 7846", check)
        self.assertNotIn("restart_veto", check, check)


if __name__ == "__main__":
    unittest.main(verbosity=2)
