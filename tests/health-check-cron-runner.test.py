#!/usr/bin/env python3
"""Tests for health-check durable schedule ownership and heartbeat."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "health-check.py"
SPEC = importlib.util.spec_from_file_location("health_check", SCRIPT)
assert SPEC and SPEC.loader
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)


class CronRunnerHealthTest(unittest.TestCase):
    def _workspace(self, root: Path, entries: list[dict]) -> Path:
        workspace = root / "workspace"
        config = workspace / "hosts" / "test-host" / "crons.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(entries))
        return workspace

    def test_codex_session_schedule_is_reported_orphaned(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = self._workspace(Path(td), [
                {"name": "main-loop", "cron": "*/5 * * * *", "prompt_skill": "proactive-loop"},
                {"name": "digest", "cron": "2 6 * * *", "prompt": "run"},
            ])
            check = health.check_cron_runner(
                workspace, host_label="test-host", runtime="codex"
            )
            self.assertEqual(check["status"], "down")
            self.assertIn("1 configured schedule(s)", check["detail"])

    def test_missing_launchd_service_is_down(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = self._workspace(Path(td), [
                {"name": "digest", "cron": "2 6 * * *", "prompt": "run", "launchd": True},
            ])
            check = health.check_cron_runner(
                workspace,
                host_label="test-host",
                runtime="codex",
                launchd_check=lambda _: {"status": "not_loaded"},
            )
            self.assertEqual(check["status"], "down")
            self.assertIn("not_loaded", check["detail"])

    def test_loaded_runner_requires_fresh_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = self._workspace(root, [
                {"name": "digest", "cron": "2 6 * * *", "prompt": "run", "launchd": True},
            ])
            state = workspace / "state" / "cron-runner-state.json"
            state.parent.mkdir(parents=True)
            state.write_text("{}")
            os.utime(state, (100, 100))
            launchd_ok = lambda _: {"status": "ok"}

            stale = health.check_cron_runner(
                workspace, "test-host", "codex", launchd_ok, now=400
            )
            fresh = health.check_cron_runner(
                workspace, "test-host", "codex", launchd_ok, now=200
            )
            self.assertEqual(stale["status"], "down")
            self.assertIn("stale", stale["detail"])
            self.assertEqual(fresh["status"], "ok")
            self.assertIn("1 durable schedule(s)", fresh["detail"])

    def test_missing_invalid_and_owner_exempt_configs_are_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            missing = health.check_cron_runner(
                workspace, "test-host", runtime="codex"
            )
            self.assertEqual(missing["status"], "ok")
            self.assertIn("no schedules", missing["detail"])

            config = workspace / "hosts" / "test-host" / "crons.json"
            config.parent.mkdir(parents=True)
            config.write_text("{")
            invalid = health.check_cron_runner(
                workspace, "test-host", runtime="codex"
            )
            self.assertEqual(invalid["status"], "fail")
            self.assertIn("cannot read", invalid["detail"])

            config.write_text("{}")
            wrong_shape = health.check_cron_runner(
                workspace, "test-host", runtime="codex"
            )
            self.assertEqual(wrong_shape["status"], "fail")
            self.assertIn("not a list", wrong_shape["detail"])

            config.write_text(json.dumps([
                {"name": "dynamic", "loop": "dynamic", "cron": "* * * * *"},
                {"name": "no-cron", "prompt": "run"},
                {"name": "codex", "cron": "* * * * *", "execution": "codex-task"},
            ]))
            exempt = health.check_cron_runner(
                workspace, "test-host", runtime="codex"
            )
            self.assertEqual(exempt["status"], "ok")
            self.assertIn("no launchd-owned", exempt["detail"])

    def test_loaded_runner_reports_missing_or_unreadable_state(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = self._workspace(Path(td), [
                {"name": "digest", "cron": "2 6 * * *", "launchd": True},
            ])
            launchd_ok = lambda _: {"status": "ok"}
            missing = health.check_cron_runner(
                workspace, "test-host", "codex", launchd_ok
            )
            self.assertEqual(missing["status"], "down")
            self.assertIn("state file is missing", missing["detail"])

            state = workspace / "state" / "cron-runner-state.json"
            state.parent.mkdir(parents=True)
            state.write_text("{}")
            with mock.patch.object(Path, "stat", side_effect=OSError("denied")):
                unreadable = health.check_cron_runner(
                    workspace, "test-host", "codex", launchd_ok
                )
            self.assertEqual(unreadable["status"], "down")
            self.assertIn("unreadable", unreadable["detail"])


if __name__ == "__main__":
    unittest.main()
