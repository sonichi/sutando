#!/usr/bin/env python3
"""Non-ARMED pin verdicts must surface on normal service paths, not vanish.

evaluate() reports ORPHAN/MISMATCH/EXPIRED precisely so a lost pin becomes a
finding, but the credential-proxy adapter kept only armed_detail(): a healthy
replacement rendered plain `ok` and a down service plain `not running`, while
the stale pin sat silent forever. These controls drive PUBLIC
entries (check_credential_proxy, check_voice_stack) with only socket/process
seams replaced, write pins through the production writer, and pin the
probe-failure reciprocals: ([], {}) is an authoritative no-match (ORPHAN
surfaces, restarts stay available), ([], None) is a FAILED enumeration
(no ORPHAN may be fabricated, and the veto blocks any restart).

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

    def test_probe_failure_healthy_is_not_orphan_and_carries_veto(self) -> None:
        process_pins.arm_pin(self.pin_file, "credential-proxy", "111",
                             LSTART, "branch witness", EXP)
        self._port("ok")
        hc._proc_lstarts = lambda pat: ([], None)   # enumeration FAILED
        check = hc.check_credential_proxy()
        self.assertNotIn("no longer running", check["detail"], check)
        self.assertIn("could not be verified", check["detail"], check)
        self.assertIn("could not be verified", check.get("restart_veto", ""),
                      "a failed probe must NOT authorize a restart")
        self.assertEqual(check["status"], "warn", check)

    def test_probe_failure_down_service_still_vetoes_fix(self) -> None:
        process_pins.arm_pin(self.pin_file, "credential-proxy", "111",
                             LSTART, "branch witness", EXP)
        self._port("down")
        hc._proc_lstarts = lambda pat: ([], None)
        check = hc.check_credential_proxy()
        self.assertIn("could not be verified", check.get("restart_veto", ""),
                      "down + unknown pin state must not hand --fix the process")
        self.assertNotIn("no longer running", check["detail"], check)

    def test_voice_stack_healthy_replacement_surfaces_orphan(self) -> None:
        process_pins.arm_pin(self.pin_file, "voice-agent", "111",
                             LSTART, "voice witness", EXP)
        saved = (hc.resolve_voice_health_config, hc.check_voice_watchers,
                 hc.check_voice_transport, hc.check_bodhi_dist)
        hc.resolve_voice_health_config = lambda **k: {"enabled": True,
                                                      "detail": "", "error": ""}
        hc.check_voice_watchers = lambda vc: {"name": "voice-watchers",
                                              "status": "ok", "detail": ""}
        hc.check_voice_transport = lambda vc: {"name": "voice-transport",
                                               "status": "ok", "detail": ""}
        hc.check_bodhi_dist = lambda: {"name": "bodhi-dist",
                                       "status": "ok", "detail": ""}
        hc.check_port = lambda *a, **k: {"name": "voice-agent",
                                         "status": "ok", "detail": "port 9900"}
        hc._proc_lstarts = lambda pat: ([0.0], {"222": LSTART})
        try:
            rows = hc.check_voice_stack()
        finally:
            (hc.resolve_voice_health_config, hc.check_voice_watchers,
             hc.check_voice_transport, hc.check_bodhi_dist) = saved
        voice = next(r for r in rows if r["name"] == "voice-agent")
        self.assertEqual(voice["status"], "warn", voice)
        self.assertIn("no longer running", voice["detail"], voice)
        self.assertIn("port 9900", voice["detail"], voice)

    def _run_fix_on_down_voice(self, lstarts):
        """REAL check_port row (closed port) + REAL main --fix dispatch."""
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        hc._proc_lstarts = lambda pat: lstarts
        row = hc.check_port(port, "voice-agent", probe=True)
        calls = []
        saved = (hc.fix_launchd, hc.run_all_checks, sys.argv)
        hc.fix_launchd = lambda label: (calls.append(label), "restarted")[1]
        hc.run_all_checks = lambda: [row]
        sys.argv = ["health-check.py", "--fix"]
        try:
            hc.main()
        except SystemExit:
            pass
        finally:
            hc.fix_launchd, hc.run_all_checks, sys.argv = saved
        return row, calls

    def test_fix_boundary_probe_timeout_withholds_restart(self) -> None:
        process_pins.arm_pin(self.pin_file, "voice-agent", "111",
                             LSTART, "voice witness", EXP)
        row, calls = self._run_fix_on_down_voice(([], None))
        self.assertIn("could not be verified", row.get("restart_veto", ""), row)
        self.assertEqual(calls, [],
                         "a failed probe must not hand --fix the pinned process")

    def test_CONTROL_fix_boundary_authoritative_no_match_still_restarts(self) -> None:
        process_pins.arm_pin(self.pin_file, "voice-agent", "111",
                             LSTART, "voice witness", EXP)
        row, calls = self._run_fix_on_down_voice(([], {}))
        self.assertNotIn("restart_veto", row, row)
        self.assertIn("no longer running", row["detail"], row)
        self.assertEqual(calls, [hc.LAUNCHD_BACKED_CHECKS["voice-agent"]],
                         "an authoritative no-match must not block the repair")

    def test_expired_pin_never_regains_veto_through_probe_failure(self) -> None:
        # Verified-expired and probe-failed-expired must AGREE: no veto.
        for exp in ("2020-01-01T00:00:00Z",):
            r = process_pins.evaluate(
                [{"service": "s", "pid": "1", "lstart": "L",
                  "reason": "r", "expires_at": exp}], "s", None, 2e9)
            self.assertEqual(r[0][0], process_pins.EXPIRED, r)
            self.assertIsNone(process_pins.veto_detail(r), r)
        # Missing and malformed expiry are the same inversion.
        for exp in ("", "soon"):
            r = process_pins.evaluate(
                [{"service": "s", "pid": "1", "lstart": "L",
                  "reason": "r", "expires_at": exp}], "s", None, 2e9)
            self.assertEqual(r[0][0], process_pins.EXPIRED, (exp, r))
            self.assertIsNone(process_pins.veto_detail(r), (exp, r))
        # CONTROL: a still-valid pin under a failed probe keeps the veto.
        r = process_pins.evaluate(
            [{"service": "s", "pid": "1", "lstart": "L",
              "reason": "r", "expires_at": "2099-01-01T00:00:00Z"}], "s", None, 0)
        self.assertEqual(r[0][0], process_pins.PROBE_FAILED, r)
        self.assertIsNotNone(process_pins.veto_detail(r), r)

    def test_fix_boundary_expired_pin_plus_probe_failure_still_repairs(self) -> None:
        process_pins.arm_pin(self.pin_file, "voice-agent", "111",
                             LSTART, "old witness", "2026-01-01T00:00:00Z")
        row, calls = self._run_fix_on_down_voice(([], None))
        self.assertNotIn("restart_veto", row, row)
        self.assertIn("expired", row["detail"], row)
        self.assertEqual(calls, [hc.LAUNCHD_BACKED_CHECKS["voice-agent"]],
                         "an expired pin must not become eternal via a failed probe")

    def test_bridge_probe_failure_row_is_not_a_fix_candidate(self) -> None:
        from unittest import mock
        process_pins.arm_pin(self.pin_file, "slack-bridge", "111",
                             LSTART, "bridge witness", EXP)
        unknown_row = {"name": "slack-bridge", "status": "warn",
                       "detail": "process probe failed — bridge state unknown"}
        hc._apply_pin_findings(unknown_row, hc._pin_verdicts("slack-bridge", None))
        self.assertIn("could not be verified", unknown_row.get("restart_veto", ""),
                      unknown_row)
        down_row = {"name": "slack-bridge", "status": "warn",
                    "detail": "configured but not running"}
        launched = []
        with mock.patch.object(hc, "_bridge_launch_plan",
                               side_effect=lambda n: (launched.append(n), None)[1]):
            hc.fix_down_bridges([unknown_row], action="restart",
                                guard=lambda repo: (True, "t"),
                                sender=lambda m: True, notifier=lambda m: True)
            self.assertEqual(launched, [],
                             "a probe-failed row must never be a restart candidate")
            hc.fix_down_bridges([down_row], action="restart",
                                guard=lambda repo: (True, "t"),
                                sender=lambda m: True, notifier=lambda m: True)
            self.assertEqual(launched, ["slack-bridge"],
                             "the authoritative no-match row must stay a candidate")

    def test_ARMED_veto_is_visible_in_ordinary_human_output(self) -> None:
        """restart_veto alone protects --fix and leaves MANUAL restarts blind."""
        process_pins.arm_pin(self.pin_file, "credential-proxy", "222",
                             LSTART, "branch witness", EXP)
        self._port("down")
        hc._proc_lstarts = lambda pat: ([0.0], {"222": LSTART})
        check = hc.check_credential_proxy()
        self.assertIn("DO NOT RESTART", check["detail"], check)
        self.assertIn("DO NOT RESTART", check.get("restart_veto", ""), check)
        # The existing diagnosis must survive beside the veto, not be replaced.
        self.assertIn("not running (optional)", check["detail"], check)

    def test_CONTROL_unpinned_down_row_gains_no_veto_text(self) -> None:
        self._port("down")
        hc._proc_lstarts = lambda pat: ([0.0], {"222": LSTART})
        check = hc.check_credential_proxy()
        self.assertNotIn("DO NOT RESTART", check["detail"], check)
        self.assertNotIn("restart_veto", check, check)

    def test_bridge_no_match_row_keeps_orphan_and_stays_a_candidate(self) -> None:
        from unittest import mock
        process_pins.arm_pin(self.pin_file, "slack-bridge", "111",
                             LSTART, "bridge witness", EXP)
        row = {"name": "slack-bridge", "status": "warn",
               "detail": "configured but not running"}
        hc._apply_pin_findings(row, hc._pin_verdicts("slack-bridge", {}))
        self.assertIn("no longer running", row["detail"], row)
        launched = []
        with mock.patch.object(hc, "_bridge_launch_plan",
                               side_effect=lambda n: (launched.append(n), None)[1]):
            hc.fix_down_bridges([row], action="restart",
                                guard=lambda repo: (True, "t"),
                                sender=lambda m: True, notifier=lambda m: True)
        self.assertEqual(launched, ["slack-bridge"],
                         "an appended orphan note must not disqualify the repair")

    def test_armed_bridge_row_is_NOT_restarted_by_fix_down_bridges(self) -> None:
        from unittest import mock
        row = {"name": "slack-bridge", "status": "warn",
               "detail": "configured but not running",
               "restart_veto": "DO NOT RESTART slack-bridge pid 1 — witness"}
        launched = []
        with mock.patch.object(hc, "_bridge_launch_plan",
                               side_effect=lambda n: (launched.append(n), None)[1]):
            hc.fix_down_bridges([row], action="restart",
                                guard=lambda repo: (True, "t"),
                                sender=lambda m: True, notifier=lambda m: True)
        self.assertEqual(launched, [], "a pin must veto the bridge repair too")

    def test_CONTROL_no_pins_healthy_stays_plain_ok(self) -> None:
        self._port("ok")
        hc._proc_lstarts = lambda pat: ([0.0], {"222": LSTART})
        check = hc.check_credential_proxy()
        self.assertEqual(check["status"], "ok", check)
        self.assertEqual(check["detail"], "port 7846", check)
        self.assertNotIn("restart_veto", check, check)


if __name__ == "__main__":
    unittest.main(verbosity=2)
