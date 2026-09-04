#!/usr/bin/env python3
"""A non-veto pin must suppress the REMEDY, never the DIAGNOSIS.

check_voice_stack() composes pin findings into voice_check BEFORE handing it to
check_voice_watchers()/check_voice_transport(). _apply_pin_findings escalates a
healthy `ok` to `warn` for any finding, and EXPIRED/ORPHAN correctly carry no
restart_veto -- so both dependent checks took their `status != ok and no veto`
early return and stopped examining a demonstrably live voice process.

These controls drive the PUBLIC entry (check_voice_stack) with only the port,
process and log-path seams replaced, and write pins through the production
arm_pin(). They do NOT stub the two dependent checks: stubbing them is exactly
what made the existing orphan regression unable to see this interaction.

Discriminator is the DETAIL, not the status -- both paths render `warn`:
  suppressed -> _voice_dep_detail(), i.e. "voice-agent warn: ..."
  examined   -> the check's own first finding, "voice-agent.log not found"

Run: python3 tests/health-check-voice-pin-suppression.test.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-voicepin-")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import process_pins  # noqa: E402

spec = importlib.util.spec_from_file_location("hc_voicepin", REPO / "src/health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

LSTART = "Sat Aug 23 12:24:57 2026"
FUTURE = "2026-12-31T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"
EXAMINED = "voice-agent.log not found"


class NonVetoPinMustNotSuppressVoiceDiagnosis(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Path(tempfile.mkdtemp(prefix="ws-voicepin-"))
        self._saved = (hc.WORKSPACE_DIR, hc.check_port, hc._proc_lstarts,
                       hc.mark_stale_if_outdated, hc.resolve_voice_health_config,
                       hc.check_bodhi_dist, hc._voice_log_path)
        hc.WORKSPACE_DIR = self.ws
        self.pin_file = self.ws / "state" / "process-pins.json"
        self.pin_file.parent.mkdir(parents=True, exist_ok=True)
        hc.mark_stale_if_outdated = lambda *a, **k: None
        hc.resolve_voice_health_config = lambda **k: {"enabled": True, "detail": "", "error": ""}
        hc.check_bodhi_dist = lambda: {"name": "bodhi-dist", "status": "ok", "detail": ""}
        # Absent log: the cheapest deterministic proof the check RAN.
        hc._voice_log_path = lambda: self.ws / "logs" / "voice-agent.log"

    def tearDown(self) -> None:
        (hc.WORKSPACE_DIR, hc.check_port, hc._proc_lstarts,
         hc.mark_stale_if_outdated, hc.resolve_voice_health_config,
         hc.check_bodhi_dist, hc._voice_log_path) = self._saved

    def _rows(self, port_status: str, live_pid: str | None):
        hc.check_port = lambda *a, **k: {"name": "voice-agent",
                                         "status": port_status,
                                         "detail": "port 9900"}
        hc._proc_lstarts = (lambda pat: ([0.0], {live_pid: LSTART})) if live_pid \
            else (lambda pat: ([], {}))
        rows = hc.check_voice_stack()
        return {r["name"]: r for r in rows}

    def _assert_examined(self, by_name, why):
        for name in ("voice-watchers", "voice-transport"):
            self.assertEqual(by_name[name]["detail"], EXAMINED,
                             f"{name} was SUPPRESSED ({why}): {by_name[name]}")

    def _assert_suppressed(self, by_name, why):
        for name in ("voice-watchers", "voice-transport"):
            self.assertTrue(by_name[name]["detail"].startswith("voice-agent "),
                            f"{name} should defer to its dependency ({why}): {by_name[name]}")

    def test_expired_pin_does_not_suppress_a_live_process(self) -> None:
        process_pins.arm_pin(self.pin_file, "voice-agent", "111", LSTART,
                             "voice witness", PAST)
        self._assert_examined(self._rows("ok", "111"), "EXPIRED pin, same live pid")

    def test_orphan_pin_does_not_suppress_a_live_process(self) -> None:
        process_pins.arm_pin(self.pin_file, "voice-agent", "111", LSTART,
                             "voice witness", FUTURE)
        # Healthy replacement: a different pid is live, so the pin is an ORPHAN.
        self._assert_examined(self._rows("ok", "222"), "ORPHAN pin, healthy replacement")

    def test_no_pin_live_process_is_examined(self) -> None:
        """Positive control: the assertion can pass for the right reason."""
        self._assert_examined(self._rows("ok", "111"), "no pin at all")

    def test_down_service_still_defers(self) -> None:
        """Negative control: the fix must NOT make a dead service look examinable."""
        self._assert_suppressed(self._rows("down", None), "voice-agent down, no pin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
