#!/usr/bin/env python3
"""A lane whose bridge STOPPED is invisible behind a healthy primary.

`_gateway_lane_verdicts` only reports lanes with a fresh sidecar, and a bridge
that dies leaves its last record — almost always `connected: true` — in place.
So with the primary serving, `check_gateway_bridge` read `ok` for as long as a
lane stayed dead. The silence of the sidecar is the only evidence, and this
suite pins that the verdict reads it.
"""
import importlib.util
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
_spec = importlib.util.spec_from_file_location("hc", _REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass

NOW = 1_800_000_000.0
STALE = hc.GATEWAY_STATUS_MAX_AGE_S * 5


def _write(d: Path, name: str, **rec) -> None:
    (d / name).write_text(json.dumps(rec))


class GatewayStaleLanes(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_fresh_lane_is_not_stale(self):
        _write(self.d, "gateway-status.local.json", connected=True, ts=NOW - 5)
        self.assertEqual(hc._gateway_stale_lanes(state_dir=self.d, now=NOW), [])

    def test_stopped_lane_is_named_with_its_age(self):
        _write(self.d, "gateway-status.local.json", connected=True, ts=NOW - STALE)
        self.assertEqual(hc._gateway_stale_lanes(state_dir=self.d, now=NOW),
                         [("local", STALE)])

    def test_connected_true_does_not_rescue_a_stopped_lane(self):
        """The record's own claim is exactly what cannot be trusted."""
        _write(self.d, "gateway-status.dev.json", connected=True, error=None,
               last_ok_ts=NOW - STALE, ts=NOW - STALE)
        self.assertEqual([ln for ln, _ in hc._gateway_stale_lanes(state_dir=self.d, now=NOW)],
                         ["dev"])

    def test_primary_sidecar_is_not_a_lane(self):
        _write(self.d, "gateway-status.json", connected=True, ts=NOW - STALE)
        self.assertEqual(hc._gateway_stale_lanes(state_dir=self.d, now=NOW), [])

    def test_malformed_ts_is_not_reported_as_stale(self):
        _write(self.d, "gateway-status.local.json", connected=True, ts="soon")
        self.assertEqual(hc._gateway_stale_lanes(state_dir=self.d, now=NOW), [])

    def test_missing_dir_is_empty(self):
        self.assertEqual(hc._gateway_stale_lanes(state_dir=Path("/nonexistent-xyz"), now=NOW), [])


class GatewayBridgeVerdictSeesStoppedLane(unittest.TestCase):
    def _run(self, serving, lanes=(), stalled=()):
        with patch.object(hc, "_gateway_configured", return_value=True), \
             patch.object(hc, "subprocess") as sp, \
             patch.object(hc, "_gateway_lock_pids", return_value={}), \
             patch.object(hc, "_gateway_serving", return_value=serving), \
             patch.object(hc, "_gateway_status_stale_age_s", return_value=None), \
             patch.object(hc, "_gateway_lane_verdicts", return_value=list(lanes)), \
             patch.object(hc, "_gateway_stale_lanes", return_value=list(stalled)):
            sp.run.return_value = type("R", (), {"stdout": "4242\n", "returncode": 0})()
            return hc.check_gateway_bridge()

    def test_healthy_primary_no_longer_hides_a_stopped_lane(self):
        r = self._run(True, stalled=[("local", 1470.0)])
        self.assertEqual(r["status"], "warn")
        self.assertIn("local", r["detail"])
        self.assertIn("stopped writing", r["detail"])
        self.assertIn("1470s", r["detail"])

    def test_healthy_primary_with_no_stopped_lane_is_unchanged(self):
        r = self._run(True)
        self.assertEqual(r, {"name": "gateway-bridge", "status": "ok",
                             "detail": "running + connected"})

    def test_served_lane_does_not_vouch_for_a_stopped_sibling(self):
        r = self._run(None, lanes=[("dev", True, True)], stalled=[("local", 7200.0)])
        self.assertEqual(r["status"], "warn")
        self.assertIn("lane dev", r["detail"])
        self.assertIn("local (last write 2.0h ago)", r["detail"])

    def test_process_only_verdict_still_sees_a_stopped_lane(self):
        r = self._run(None, stalled=[("local", 600.0)])
        self.assertEqual(r["status"], "warn")

    def test_no_lanes_at_all_keeps_the_process_only_verdict(self):
        r = self._run(None)
        self.assertEqual(r, {"name": "gateway-bridge", "status": "ok", "detail": "running"})


class ShippedPathReadsTheRealSidecar(unittest.TestCase):
    """Same verdict without mocking the helper: the file on disk is the input."""

    def test_stale_lane_file_next_to_a_serving_primary_warns(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        now = time.time()
        _write(d, "gateway-status.json", connected=True, last_ok_ts=now, ts=now)
        _write(d, "gateway-status.local.json", connected=True, last_ok_ts=now - STALE,
               ts=now - STALE)
        with patch.object(hc, "_gateway_configured", return_value=True), \
             patch.object(hc, "subprocess") as sp, \
             patch.object(hc, "_gateway_lock_pids", return_value={}), \
             patch.object(hc, "status_read_path",
                          side_effect=lambda name, *_a, **_k: d / name):
            sp.run.return_value = type("R", (), {"stdout": "4242\n", "returncode": 0})()
            r = hc.check_gateway_bridge()
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("lane local", r["detail"])


if __name__ == "__main__":
    unittest.main()
