#!/usr/bin/env python3
"""
Tests for skills/quota-tracker/scripts/read-quota.py

Coverage:
  main() human-readable — allowed status, correct % display
  main() --json         — all expected keys present, values correct
  main() --gate         — exits 0 when allowed, exits 1 when exhausted
  main() edge cases     — zero utilization, full utilization, missing resets

Run: python3 skills/quota-tracker/tests/read-quota.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent.parent.parent

# Inject fake workspace_default before loading the module so the module-level
# status_read_path call doesn't try to open the real workspace.
TMPDIR = Path(tempfile.mkdtemp())
_FAKE_QUOTA = TMPDIR / "quota-state.json"
_FAKE_QUOTA.write_text(json.dumps({"headers": {}}))

_fake_wd = type(sys)("workspace_default")
_fake_wd.status_read_path = lambda name: _FAKE_QUOTA
sys.modules["workspace_default"] = _fake_wd

_spec = importlib.util.spec_from_file_location(
    "read_quota",
    REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py",
)
rq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rq)


def _write_quota(headers: dict) -> None:
    """Write a quota-state.json fixture and point the module at it."""
    path = TMPDIR / "quota-state.json"
    path.write_text(json.dumps({"headers": headers}))
    rq.QUOTA_FILE = path


_ALLOWED_HEADERS = {
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.01",
    "anthropic-ratelimit-unified-7d-utilization": "0.25",
    "anthropic-ratelimit-unified-5h-reset": "1746086400",
    "anthropic-ratelimit-unified-7d-reset": "1746259200",
}

_EXHAUSTED_HEADERS = {
    "anthropic-ratelimit-unified-status": "blocked",
    "anthropic-ratelimit-unified-5h-utilization": "1.0",
    "anthropic-ratelimit-unified-7d-utilization": "0.9",
    "anthropic-ratelimit-unified-5h-reset": "1746086400",
    "anthropic-ratelimit-unified-7d-reset": "1746259200",
}


class TestMainHumanReadable(unittest.TestCase):
    def setUp(self):
        _write_quota(_ALLOWED_HEADERS)
        self._orig_argv = sys.argv[:]

    def tearDown(self):
        sys.argv = self._orig_argv

    def _run(self) -> str:
        sys.argv = ["read-quota.py"]
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            rq.main()
            return mock_out.getvalue()

    def test_status_line_present(self):
        out = self._run()
        self.assertIn("Status:", out)
        self.assertIn("allowed", out)

    def test_5h_window_shown(self):
        out = self._run()
        self.assertIn("5h window:", out)
        self.assertIn("1% used", out)
        self.assertIn("99% remaining", out)

    def test_7d_window_shown(self):
        out = self._run()
        self.assertIn("7d window:", out)
        self.assertIn("25% used", out)
        self.assertIn("75% remaining", out)

    def test_reset_times_shown(self):
        out = self._run()
        self.assertIn("Resets:", out)

    def test_zero_utilization(self):
        _write_quota({
            **_ALLOWED_HEADERS,
            "anthropic-ratelimit-unified-5h-utilization": "0.0",
            "anthropic-ratelimit-unified-7d-utilization": "0.0",
        })
        out = self._run()
        self.assertIn("0% used", out)
        self.assertIn("100% remaining", out)

    def test_full_utilization(self):
        _write_quota({
            **_ALLOWED_HEADERS,
            "anthropic-ratelimit-unified-5h-utilization": "1.0",
        })
        out = self._run()
        self.assertIn("100% used", out)
        self.assertIn("0% remaining", out)

    def test_missing_reset_no_crash(self):
        _write_quota({
            "anthropic-ratelimit-unified-status": "allowed",
            "anthropic-ratelimit-unified-5h-utilization": "0.5",
            "anthropic-ratelimit-unified-7d-utilization": "0.5",
        })
        out = self._run()
        self.assertIn("5h window:", out)


class TestMainJsonMode(unittest.TestCase):
    def setUp(self):
        _write_quota(_ALLOWED_HEADERS)
        self._orig_argv = sys.argv[:]

    def tearDown(self):
        sys.argv = self._orig_argv

    def _run_json(self) -> dict:
        sys.argv = ["read-quota.py", "--json"]
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            rq.main()
            return json.loads(mock_out.getvalue())

    def test_returns_valid_json(self):
        result = self._run_json()
        self.assertIsInstance(result, dict)

    def test_status_field(self):
        result = self._run_json()
        self.assertEqual(result["status"], "allowed")

    def test_available_true_when_allowed(self):
        result = self._run_json()
        self.assertTrue(result["available"])

    def test_available_false_when_blocked(self):
        _write_quota(_EXHAUSTED_HEADERS)
        result = self._run_json()
        self.assertFalse(result["available"])

    def test_remaining_5h_pct(self):
        result = self._run_json()
        self.assertEqual(result["remaining_5h_pct"], 99)

    def test_remaining_7d_pct(self):
        result = self._run_json()
        self.assertEqual(result["remaining_7d_pct"], 75)

    def test_utilization_fields_present(self):
        result = self._run_json()
        self.assertIn("utilization_5h", result)
        self.assertIn("utilization_7d", result)

    def test_reset_fields_present(self):
        result = self._run_json()
        self.assertIn("reset_5h", result)
        self.assertIn("reset_7d", result)

    def test_full_utilization_zero_remaining(self):
        _write_quota({
            **_ALLOWED_HEADERS,
            "anthropic-ratelimit-unified-5h-utilization": "1.0",
        })
        result = self._run_json()
        self.assertEqual(result["remaining_5h_pct"], 0)

    def test_missing_resets_no_crash(self):
        _write_quota({
            "anthropic-ratelimit-unified-status": "allowed",
            "anthropic-ratelimit-unified-5h-utilization": "0.5",
            "anthropic-ratelimit-unified-7d-utilization": "0.5",
        })
        result = self._run_json()
        self.assertNotIn("reset_5h", result)
        self.assertNotIn("reset_7d", result)


class TestMainGateMode(unittest.TestCase):
    def setUp(self):
        self._orig_argv = sys.argv[:]

    def tearDown(self):
        sys.argv = self._orig_argv

    def test_gate_exits_0_when_allowed(self):
        _write_quota(_ALLOWED_HEADERS)
        sys.argv = ["read-quota.py", "--gate"]
        with self.assertRaises(SystemExit) as ctx:
            rq.main()
        self.assertEqual(ctx.exception.code, 0)

    def test_gate_exits_1_when_blocked(self):
        _write_quota(_EXHAUSTED_HEADERS)
        sys.argv = ["read-quota.py", "--gate"]
        with self.assertRaises(SystemExit) as ctx:
            rq.main()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
