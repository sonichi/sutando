#!/usr/bin/env python3
"""A broken process probe must not report the gateway bridge as DOWN.

`check_gateway_bridge` read pgrep directly and collapsed every non-zero exit to
an empty pid list. pgrep exits 1 for "ran, matched nothing" but 2/3 when it
cannot run at all, so a broken probe and a genuinely absent bridge produced the
identical `GATEWAY_DOWN_DETAIL` row. That is not a cosmetic mislabel: the row
carries the mobile-messages-are-stranded copy and is what a reader acts on.

Observed on this fleet — with `sysmond` unavailable, pgrep returned rc 3 and a
deliberately-absent pattern returned the identical bytes, so health-check
reported live bridges as down and the record had to warn readers off its own
output. The sibling bridge loop already distinguishes the two through
`probe_pids()` and emits an explicit unknown row; this pins the gateway probe to
the same contract.

The genuinely-absent case is asserted alongside it on purpose: a fix that
reported UNKNOWN for everything would also remove the false DOWN, and would be
worse than the bug — the outage this check exists for (2026-07-10, three silent
days) would stop being reported at all.

Run: python3 tests/health-check-gateway-probe-unknown.test.py
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("CLAUDE_CONFIG_DIR", tempfile.mkdtemp(prefix="ccd-gw-probe-"))

REPO = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("hc_gw", REPO / "src/health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(pids, probe_ok):
    """check_gateway_bridge() with only the process probe faked."""
    mod = _load()
    mod._gateway_configured = lambda: True
    mod.probe_pids = lambda _pattern: (pids, probe_ok)
    return mod.check_gateway_bridge() or {}


class GatewayProbeUnknown(unittest.TestCase):
    def test_broken_probe_is_unknown_not_down(self):
        row = _row([], probe_ok=False)
        mod = _load()
        self.assertEqual(row.get("status"), "warn")
        self.assertIn("unknown", row.get("detail", "").lower())
        self.assertNotEqual(
            row.get("detail"), mod.GATEWAY_DOWN_DETAIL,
            "a probe that could not run must not claim the bridge is down",
        )

    def test_absent_bridge_still_reports_down(self):
        """Control. Without this, reporting UNKNOWN for everything would pass."""
        mod = _load()
        row = _row([], probe_ok=True)
        self.assertEqual(row.get("status"), "warn")
        self.assertEqual(
            row.get("detail"), mod.GATEWAY_DOWN_DETAIL,
            "a probe that RAN and found nothing must still report the outage",
        )

    def test_running_bridge_is_not_reported_down(self):
        """Control. A single live pid must not trip either warn branch."""
        mod = _load()
        row = _row(["4242"], probe_ok=True)
        self.assertNotEqual(row.get("detail"), mod.GATEWAY_DOWN_DETAIL)
        self.assertNotIn("unknown", row.get("detail", "").lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
