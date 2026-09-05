#!/usr/bin/env python3
"""A live gateway PID may belong to a non-primary lane.

The lane selector runs exactly ONE lane and parks the others, so on a lane-only
host `state/gateway-status.json` is never written. An absent primary sidecar is
not evidence of health, and the running lane's own sidecar is the signal.
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


class GatewayLaneVerdicts(unittest.TestCase):
    def _dir(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def _lane(self, d: Path, lane: str, *, connected: bool,
              last_ok: "float | None", age_s: float = 5.0) -> None:
        (d / f"gateway-status.{lane}.json").write_text(json.dumps(
            {"connected": connected, "ts": time.time() - age_s, "last_ok_ts": last_ok}))

    def test_never_polled_lane_is_reported(self):
        d = self._dir()
        self._lane(d, "ag2space_dlocal", connected=False, last_ok=None)
        v = hc._gateway_lane_verdicts(state_dir=d)
        self.assertEqual(v, [("ag2space_dlocal", False, False)])

    def test_worked_then_died_is_distinguished_from_never_polled(self):
        """Different failures: one is a misconfigured endpoint, the other an
        outage. A probe that cannot tell them apart sends people the wrong way."""
        d = self._dir()
        self._lane(d, "never", connected=False, last_ok=None)
        self._lane(d, "died", connected=False, last_ok=time.time() - 9000)
        got = {ln: ever for ln, _, ever in hc._gateway_lane_verdicts(state_dir=d)}
        self.assertEqual(got, {"never": False, "died": True})

    def test_connected_without_a_completed_poll_is_not_serving(self):
        """The discriminating case, and the state this probe was written for: a
        lane whose sidecar says connected:true but that has never completed a
        poll. `connected` alone is what a dead bridge's last write leaves
        behind, so reading it directly reports a parked lane as healthy."""
        d = self._dir()
        self._lane(d, "ag2space_dlocal", connected=True, last_ok=None)
        self.assertEqual(hc._gateway_lane_verdicts(state_dir=d),
                         [("ag2space_dlocal", False, False)])

    def test_unreadable_state_dir_yields_no_lanes_rather_than_raising(self):
        """A probe must not take the whole health check down with it. An
        unreadable state dir is no evidence about the lanes, not an error."""
        d = self._dir()
        self._lane(d, "ag2space_dlocal", connected=True, last_ok=None)
        with patch.object(Path, "glob", side_effect=OSError("permission denied")):
            self.assertEqual(hc._gateway_lane_verdicts(state_dir=d), [])

    def test_stale_lane_sidecar_is_ignored(self):
        d = self._dir()
        self._lane(d, "old", connected=False, last_ok=None,
                   age_s=hc.GATEWAY_STATUS_MAX_AGE_S + 60)
        self.assertEqual(hc._gateway_lane_verdicts(state_dir=d), [])

    def test_primary_sidecar_is_not_treated_as_a_lane(self):
        """`gateway-status.json` has no lane segment; globbing it in would
        invent a lane named after the file and double-count primary."""
        d = self._dir()
        (d / "gateway-status.json").write_text(json.dumps(
            {"connected": True, "ts": time.time(), "last_ok_ts": time.time()}))
        self.assertEqual(hc._gateway_lane_verdicts(state_dir=d), [])

    def test_malformed_and_unreadable_lanes_do_not_raise(self):
        d = self._dir()
        (d / "gateway-status.bad.json").write_text("{not json")
        (d / "gateway-status.nots.json").write_text(json.dumps({"connected": True}))
        (d / "gateway-status.boolts.json").write_text(json.dumps(
            {"connected": True, "ts": True}))
        self.assertEqual(hc._gateway_lane_verdicts(state_dir=d), [])

    def test_absent_state_dir_is_empty_not_an_error(self):
        self.assertEqual(hc._gateway_lane_verdicts(state_dir=Path("/nonexistent-xyz")), [])


class GatewayBridgeVerdictUsesLanes(unittest.TestCase):
    """The caller: a live PID plus no primary opinion must not read as ok when a
    fresh lane sidecar says otherwise."""

    def _run(self, lanes):
        with patch.object(hc, "_gateway_configured", return_value=True), \
             patch.object(hc, "subprocess") as sp, \
             patch.object(hc, "_gateway_lock_pids", return_value={}), \
             patch.object(hc, "_gateway_serving", return_value=None), \
             patch.object(hc, "_gateway_status_stale_age_s", return_value=None), \
             patch.object(hc, "_gateway_lane_verdicts", return_value=lanes):
            sp.run.return_value = type("R", (), {"stdout": "4242\n", "returncode": 0})()
            return hc.check_gateway_bridge()

    def test_never_polled_lane_warns_instead_of_ok(self):
        r = self._run([("ag2space_dlocal", False, False)])
        self.assertEqual(r["status"], "warn")
        self.assertIn("never completed a poll", r["detail"])
        self.assertIn("ag2space_dlocal", r["detail"])

    def test_disconnected_lane_that_once_worked_warns(self):
        r = self._run([("primaryish", False, True)])
        self.assertEqual(r["status"], "warn")
        self.assertIn("not connected", r["detail"])
        self.assertNotIn("never completed", r["detail"])

    def test_serving_lane_is_ok_and_names_the_lane(self):
        r = self._run([("ag2space_dlocal", True, True)])
        self.assertEqual(r["status"], "ok")
        self.assertIn("ag2space_dlocal", r["detail"])

    def test_no_lane_sidecars_keeps_the_process_only_verdict(self):
        """Regression guard: hosts with no lanes must be untouched."""
        r = self._run([])
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["detail"], "running")


if __name__ == "__main__":
    unittest.main(verbosity=1)
