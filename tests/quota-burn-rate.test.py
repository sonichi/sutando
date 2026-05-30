#!/usr/bin/env python3
"""Tests for burn-rate EWMA in skills/quota-tracker/scripts/read-quota.py (PR #1319).

Covers _load_burn_history, _save_burn_history, and _update_burn_rate.

Run: python3 tests/quota-burn-rate.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
_SCRIPT = REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"


def _load_module(workspace: Path):
    """Load read-quota.py with a controlled workspace and a dummy quota-state.json."""
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    sys.modules.pop("read_quota", None)
    sys.modules.pop("read_quota_under_test", None)

    # The module exits early if quota-state.json is missing. Create a minimal one.
    state_dir = workspace / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    quota_file = state_dir / "quota-state.json"
    if not quota_file.exists():
        quota_file.write_text(json.dumps({"headers": {
            "anthropic-ratelimit-unified-status": "allowed",
            "anthropic-ratelimit-unified-5h-utilization": "0.1",
            "anthropic-ratelimit-unified-remaining": "900",
            "anthropic-ratelimit-unified-reset": "2026-05-28T12:50:00Z",
        }}))

    spec = importlib.util.spec_from_file_location("read_quota_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "src"))
    spec.loader.exec_module(mod)
    return mod


class TestBurnHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quota-burn-test-"))
        self.mod = _load_module(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_returns_empty_on_missing_file(self):
        self.mod.BURN_HISTORY_FILE = self.tmp / "state" / "nonexistent.json"
        result = self.mod._load_burn_history()
        self.assertEqual(result, {})

    def test_save_and_load_roundtrip(self):
        path = self.tmp / "state" / "burn.json"
        self.mod.BURN_HISTORY_FILE = path
        data = {"last_read_ts": 1234567.0, "burn_rate_5h_ewma": 0.002, "burn_samples": 5}
        self.mod._save_burn_history(data)
        loaded = self.mod._load_burn_history()
        self.assertEqual(loaded["burn_samples"], 5)
        self.assertAlmostEqual(loaded["burn_rate_5h_ewma"], 0.002)

    def test_load_returns_empty_on_corrupt_file(self):
        path = self.tmp / "state" / "corrupt.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json {{")
        self.mod.BURN_HISTORY_FILE = path
        self.assertEqual(self.mod._load_burn_history(), {})


class TestUpdateBurnRate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quota-burn-test-"))
        self.mod = _load_module(self.tmp)
        self.burn_file = self.tmp / "state" / "quota-burn-history.json"
        self.mod.BURN_HISTORY_FILE = self.burn_file
        self._t = 1_000_000.0  # controllable wall-clock

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, util: float, advance_s: float = 0.0):
        self._t += advance_s
        with patch.object(self.mod.time, "time", return_value=self._t):
            return self.mod._update_burn_rate(util)

    def test_first_call_returns_none(self):
        result = self._call(0.10)
        self.assertIsNone(result)

    def test_second_call_gap_too_small_returns_none(self):
        self._call(0.10)
        # 60s < MIN_GAP_S (120s) → sample skipped, still no EWMA
        result = self._call(0.12, advance_s=60)
        self.assertIsNone(result)

    def test_second_call_valid_gap_one_sample_returns_none(self):
        self._call(0.10)
        # 300s is within [120, 7200], delta positive → 1 sample → still None (need ≥2)
        result = self._call(0.15, advance_s=300)
        self.assertIsNone(result)

    def test_two_valid_samples_returns_forecast_dict(self):
        self._call(0.10)
        self._call(0.15, advance_s=300)   # sample 1
        result = self._call(0.20, advance_s=300)  # sample 2 → forecast
        self.assertIsNotNone(result)
        self.assertIn("burn_rate_pct_per_pass", result)
        self.assertIn("burn_samples", result)
        self.assertIn("estimated_passes_left", result)
        self.assertIn("estimated_minutes_left", result)
        self.assertEqual(result["burn_samples"], 2)

    def test_gap_too_large_skips_sample(self):
        self._call(0.10)
        # 8000s > MAX_GAP_S (7200s) → skipped
        result = self._call(0.15, advance_s=8000)
        self.assertIsNone(result)

    def test_decreasing_util_skips_sample(self):
        self._call(0.20)
        # delta < 0 (quota reset or read error) → sample excluded
        result = self._call(0.10, advance_s=300)
        self.assertIsNone(result)

    def test_ewma_smoothing_applied(self):
        alpha = self.mod._EWMA_ALPHA
        # First valid sample: per_pass = 0.05 * (300/300) = 0.05 → ewma = 0.05
        self._call(0.10)
        self._call(0.15, advance_s=300)  # ewma initialised to 0.05

        # Second valid sample: per_pass = 0.05 → ewma = alpha*0.05 + (1-alpha)*0.05 = 0.05
        result = self._call(0.20, advance_s=300)
        self.assertIsNotNone(result)
        expected_ewma_pct = round(0.05 * 100, 2)
        self.assertAlmostEqual(result["burn_rate_pct_per_pass"], expected_ewma_pct, places=1)

    def test_estimated_passes_left_positive(self):
        self._call(0.10)
        self._call(0.15, advance_s=300)
        result = self._call(0.20, advance_s=300)
        self.assertIsNotNone(result)
        self.assertGreater(result["estimated_passes_left"], 0)
        # estimated_minutes_left ≈ passes_left * 5
        self.assertAlmostEqual(
            result["estimated_minutes_left"],
            round(result["estimated_passes_left"] * 5),
            delta=1,
        )


if __name__ == "__main__":
    unittest.main()
