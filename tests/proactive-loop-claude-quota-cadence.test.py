#!/usr/bin/env python3
"""Regression tests for Claude proactive-loop quota-aware cadence."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "proactive-loop" / "scripts" / "claude-quota-cadence.py"
SPEC = importlib.util.spec_from_file_location("claude_quota_cadence", SCRIPT)
cadence = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cadence)
PROXY_URL = "http://localhost:7846"


def _quota(utilization: float, *, available: bool = True, status: str = "allowed") -> dict:
    return {
        "available": available,
        "utilization_7d": utilization,
        "headers": {"anthropic-ratelimit-unified-status": status},
    }


def _crons(expression: str = "*/5 * * * *") -> list[dict]:
    return [{
        "name": "main-loop",
        "cron": expression,
        "prompt_skill": "proactive-loop",
    }]


class ClaudeQuotaCadenceTest(unittest.TestCase):
    def test_proactive_loop_reconciles_the_registered_cron(self) -> None:
        text = (REPO / "skills" / "proactive-loop" / "SKILL.md").read_text()
        self.assertIn("claude-quota-cadence.py --json", text)
        self.assertIn("CronDelete", text)
        self.assertIn("CronCreate", text)
        self.assertLess(text.index("`CronCreate` the new job"), text.index("Then `CronDelete`"))
        self.assertIn("exactly one `/proactive-loop` job remains", text)

    def test_boot_registration_uses_the_effective_cron(self) -> None:
        text = (REPO / "skills" / "schedule-crons" / "SKILL.md").read_text()
        self.assertIn("claude-quota-cadence.py --json", text)
        self.assertIn("effective_cron", text)

    def test_below_threshold_keeps_configured_cadence(self) -> None:
        result = cadence.choose_cadence(
            _quota(0.79), _crons(), quota_age_seconds=5, base_url=PROXY_URL,
        )
        self.assertFalse(result["throttled"])
        self.assertEqual(result["effective_cron"], "*/5 * * * *")

    def test_threshold_throttles_to_thirty_minutes(self) -> None:
        result = cadence.choose_cadence(
            _quota(0.80), _crons(), quota_age_seconds=5, base_url=PROXY_URL,
        )
        self.assertTrue(result["throttled"])
        self.assertEqual(result["effective_cron"], "*/30 * * * *")

    def test_reset_restores_exact_configured_cadence(self) -> None:
        high = cadence.choose_cadence(
            _quota(0.95), _crons("*/15 * * * *"), quota_age_seconds=5, base_url=PROXY_URL,
        )
        reset = cadence.choose_cadence(
            _quota(0.02), _crons("*/15 * * * *"), quota_age_seconds=5, base_url=PROXY_URL,
        )
        self.assertEqual(high["effective_cron"], "*/30 * * * *")
        self.assertEqual(reset["effective_cron"], "*/15 * * * *")

    def test_stale_or_missing_quota_fails_safe_to_thirty_minutes(self) -> None:
        stale = cadence.choose_cadence(
            _quota(0.10), _crons(), quota_age_seconds=31 * 60, base_url=PROXY_URL,
        )
        missing = cadence.choose_cadence(None, _crons(), quota_age_seconds=None, base_url=PROXY_URL)
        self.assertEqual(stale["effective_cron"], "*/30 * * * *")
        self.assertEqual(missing["effective_cron"], "*/30 * * * *")
        self.assertEqual(stale["reason"], "quota-unavailable")

    def test_header_only_quota_shape_is_supported(self) -> None:
        result = cadence.choose_cadence({"available": True, "headers": {
            "anthropic-ratelimit-unified-status": "allowed",
            "anthropic-ratelimit-unified-7d-utilization": "0.81",
        }}, _crons(), quota_age_seconds=5, base_url=PROXY_URL)
        self.assertEqual(result["effective_cron"], "*/30 * * * *")

    def test_direct_prompt_loop_restores_its_configured_cadence(self) -> None:
        crons = [{
            "name": "custom-loop",
            "cron": "*/5 * * * *",
            "prompt": "/proactive-loop",
        }]
        result = cadence.choose_cadence(
            _quota(0.10), crons, quota_age_seconds=5, base_url=PROXY_URL,
        )
        self.assertEqual(result["effective_cron"], "*/5 * * * *")

    def test_never_speeds_up_a_slower_or_custom_schedule(self) -> None:
        slow = cadence.choose_cadence(
            _quota(0.95), _crons("*/45 * * * *"), quota_age_seconds=5, base_url=PROXY_URL,
        )
        custom = cadence.choose_cadence(
            _quota(0.95), _crons("5,35 * * * *"), quota_age_seconds=5, base_url=PROXY_URL,
        )
        self.assertEqual(slow["effective_cron"], "*/45 * * * *")
        self.assertEqual(custom["effective_cron"], "5,35 * * * *")
        self.assertEqual(custom["reason"], "unsupported-normal-cron")

    def test_path_reader_uses_mtime_staleness(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            quota = root / "quota-state.json"
            crons = root / "crons.json"
            quota.write_text(json.dumps(_quota(0.10)))
            crons.write_text(json.dumps(_crons()))
            now = time.time()
            old = now - 31 * 60
            os.utime(quota, (old, old))
            result = cadence.evaluate_paths(quota, crons, now=now, base_url=PROXY_URL)
        self.assertEqual(result["effective_cron"], "*/30 * * * *")

    def test_rejected_proxy_flag_and_unrouted_telemetry_fail_closed(self) -> None:
        rejected = cadence.choose_cadence(
            _quota(0.10, status="rejected"), _crons(), quota_age_seconds=5,
            base_url=PROXY_URL,
        )
        unavailable = cadence.choose_cadence(
            _quota(0.10, available=False), _crons(), quota_age_seconds=5,
            base_url=PROXY_URL,
        )
        unrouted = cadence.choose_cadence(
            _quota(0.10), _crons(), quota_age_seconds=5, base_url=None,
        )
        for result in (rejected, unavailable, unrouted):
            self.assertFalse(result["available"])
            self.assertEqual(result["effective_cron"], "*/30 * * * *")

    def test_runtime_cron_path_is_the_canonical_host_path(self) -> None:
        sys.path.insert(0, str(REPO / "src"))
        import util_paths
        import workspace_default

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(workspace_default, "resolve_workspace", return_value=Path(td)), \
                mock.patch.object(util_paths, "_host_label", return_value="test-host"):
            quota_path, crons_path = cadence._runtime_paths()
        self.assertEqual(quota_path, Path(td) / "state" / "quota-state.json")
        self.assertEqual(crons_path, Path(td) / "hosts" / "test-host" / "crons.json")

    def test_cli_round_trip_throttles_then_restores_configured_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            quota = root / "quota-state.json"
            crons = root / "hosts" / "test-host" / "crons.json"
            crons.parent.mkdir(parents=True)
            crons.write_text(json.dumps(_crons()))

            outputs = []
            with mock.patch.object(cadence, "_runtime_paths", return_value=(quota, crons)), \
                    mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": PROXY_URL}), \
                    mock.patch("sys.argv", [str(SCRIPT), "--json"]):
                for utilization in (0.85, 0.10):
                    quota.write_text(json.dumps(_quota(utilization)))
                    stream = io.StringIO()
                    with redirect_stdout(stream):
                        self.assertEqual(cadence.main(), 0)
                    outputs.append(json.loads(stream.getvalue()))
            self.assertEqual(len(json.loads(crons.read_text())), 1)

        self.assertEqual(outputs[0]["effective_cron"], "*/30 * * * *")
        self.assertEqual(outputs[1]["effective_cron"], "*/5 * * * *")

    def test_cli_human_output_and_malformed_inputs_fail_safe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            quota = root / "quota-state.json"
            crons = root / "crons.json"
            quota.write_text("not-json")
            crons.write_text("not-json")
            stream = io.StringIO()
            with mock.patch.object(cadence, "_runtime_paths", return_value=(quota, crons)), \
                    mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": PROXY_URL}), \
                    mock.patch("sys.argv", [str(SCRIPT)]), redirect_stdout(stream):
                self.assertEqual(cadence.main(), 0)
        self.assertIn("*/30 * * * * (quota-unavailable, 7d=None)", stream.getvalue())

    def test_malformed_values_and_cron_entries_use_safe_defaults(self) -> None:
        for value in (True, object(), "not-a-number", float("inf"), -0.1, 1.1):
            self.assertIsNone(cadence._finite_fraction(value))
        self.assertIsNone(cadence._utilization_7d([]))
        self.assertIsNone(cadence._utilization_7d({"headers": []}))
        self.assertEqual(cadence._normal_cron(None), cadence.FALLBACK_NORMAL_CRON)
        self.assertEqual(
            cadence._normal_cron([None, {"name": "other"}, {"name": "main-loop", "cron": 5}]),
            cadence.FALLBACK_NORMAL_CRON,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
