#!/usr/bin/env python3
"""`read-quota.py` must not present another session's numbers as this one's budget.

Run: python3 tests/quota-read-not-routed.test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SCRIPT = REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"

_BURN = {
    "burn_rate_pct_per_pass": 1.25,
    "burn_samples": 9,
    "binding_window": "5h",
    "estimated_passes_left": 40.0,
    "estimated_minutes_left": 200,
    "unforecast_windows": [],
}


def _load_module(workspace: Path):
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"
    sys.modules.pop("read_quota_under_test", None)
    state = workspace / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "quota-state.json").write_text(json.dumps({"headers": {
        "anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-5h-utilization": "0.12",
        "anthropic-ratelimit-unified-7d-utilization": "0.55",
    }}))
    spec = importlib.util.spec_from_file_location("read_quota_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "src"))
    spec.loader.exec_module(mod)
    return mod


class NotRoutedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quota-routed-"))
        self._env = dict(os.environ)
        self.mod = _load_module(self.tmp)
        self.mod._update_burn_rate = lambda *a, **k: dict(_BURN)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, routed: bool, argv=("read-quota.py",)) -> str:
        if routed:
            os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8787"
        else:
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        buf = io.StringIO()
        old = sys.argv
        sys.argv = list(argv)
        try:
            with contextlib.redirect_stdout(buf):
                self.mod.main()
        finally:
            sys.argv = old
        return buf.getvalue()

    # --- the banner ----------------------------------------------------------

    def test_unrouted_says_the_numbers_are_not_this_session(self) -> None:
        out = self._run(routed=False)
        self.assertIn("NOT ROUTED", out)
        self.assertIn("ANTHROPIC_BASE_URL", out)

    def test_routed_prints_no_banner(self) -> None:
        """Control: without this, the banner test passes on a always-print bug."""
        out = self._run(routed=True)
        self.assertNotIn("NOT ROUTED", out)

    # --- the forecast --------------------------------------------------------

    def test_unrouted_suppresses_the_forecast(self) -> None:
        out = self._run(routed=False)
        self.assertIn("SUPPRESSED", out)
        self.assertNotIn("Burn rate:", out)

    def test_routed_still_prints_the_forecast(self) -> None:
        """Control: suppression must come from routing, not from losing the burn."""
        out = self._run(routed=True)
        self.assertIn("Burn rate:", out)
        self.assertNotIn("SUPPRESSED", out)

    # --- the machine-readable half -------------------------------------------

    def test_json_carries_routed_false(self) -> None:
        out = self._run(routed=False, argv=("read-quota.py", "--json"))
        self.assertIs(json.loads(out)["routed"], False)

    def test_json_carries_routed_true(self) -> None:
        out = self._run(routed=True, argv=("read-quota.py", "--json"))
        self.assertIs(json.loads(out)["routed"], True)

    # --- the window the numbers still describe -------------------------------

    def test_unrouted_still_reports_the_raw_windows(self) -> None:
        """Not-routed is a provenance warning, not a reason to hide the data."""
        out = self._run(routed=False)
        self.assertIn("5h window", out)
        self.assertIn("7d window", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
