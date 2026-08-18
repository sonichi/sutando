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

    def test_a_killed_invocation_with_fresh_state_is_degraded_not_down(self):
        """`check_launchd` reports the LAST invocation, not whether work happens.

        Measured on a live host: launchd showed `exit=-9` (SIGKILL, code signing)
        while the runner wrote state every ~120s and that morning's briefing was
        emitted on the minute. The old early return called that `down`.
        """
        killed = lambda _: {"status": "stopped", "detail": "pid=- exit=-9"}
        with tempfile.TemporaryDirectory() as td:
            workspace = self._workspace(Path(td), [
                {"name": "digest", "cron": "2 6 * * *", "launchd": True},
            ])
            state = workspace / "state" / "cron-runner-state.json"
            state.parent.mkdir(parents=True)
            state.write_text("{}")
            mtime = state.stat().st_mtime

            fresh = health.check_cron_runner(
                workspace, "test-host", "codex", killed, now=mtime + 120
            )
            self.assertEqual(fresh["status"], "warn", fresh["detail"])
            self.assertIn("120s ago", fresh["detail"])
            self.assertIn("schedules still fire", fresh["detail"])
            self.assertIn("exit=-9", fresh["detail"])

            # Past the freshness bound nothing proves work is happening, so the
            # original verdict must survive.
            stale = health.check_cron_runner(
                workspace, "test-host", "codex", killed, now=mtime + 181
            )
            self.assertEqual(stale["status"], "down")
            self.assertIn("launchd is stopped", stale["detail"])

    def test_a_killed_invocation_with_no_state_file_stays_down(self):
        """Regression guard: the freshness read moved ABOVE the launchd branch,
        so a missing file must not be described as if the runner were loaded."""
        killed = lambda _: {"status": "not_loaded", "detail": "not found"}
        with tempfile.TemporaryDirectory() as td:
            workspace = self._workspace(Path(td), [
                {"name": "digest", "cron": "2 6 * * *", "launchd": True},
            ])
            res = health.check_cron_runner(workspace, "test-host", "codex", killed)
            self.assertEqual(res["status"], "down")
            self.assertIn("launchd is not_loaded", res["detail"])
            self.assertNotIn("runner loaded", res["detail"])

    def test_a_future_dated_state_file_never_buys_a_downgrade(self):
        """A clock step must not read as freshness — on EITHER branch.

        Guarding only the render (`int(max(age, 0))`) turned a day-old file into
        the literal text "wrote state 0s ago ... schedules still fire", which is
        worse than a missing warning because it reads as freshly measured.
        """
        killed = lambda _: {"status": "stopped", "detail": "pid=- exit=-9"}
        loaded = lambda _: {"status": "ok", "detail": "pid=123"}
        with tempfile.TemporaryDirectory() as td:
            workspace = self._workspace(Path(td), [
                {"name": "digest", "cron": "2 6 * * *", "launchd": True},
            ])
            state = workspace / "state" / "cron-runner-state.json"
            state.parent.mkdir(parents=True)
            state.write_text("{}")
            mtime = state.stat().st_mtime

            for label, probe in (("killed", killed), ("loaded", loaded)):
                res = health.check_cron_runner(
                    workspace, "test-host", "codex", probe, now=mtime - 86400
                )
                self.assertEqual(res["status"], "down", f"{label}: {res['detail']}")
                self.assertIn("future-dated", res["detail"], label)
                # The exact regression: a day-old file rendered as a fresh write.
                self.assertNotIn("wrote state", res["detail"], label)
                self.assertNotIn("schedules still fire", res["detail"], label)

            # One second in the future is still a clock step, not a fresh write.
            edge = health.check_cron_runner(
                workspace, "test-host", "codex", killed, now=mtime - 1
            )
            self.assertEqual(edge["status"], "down", edge["detail"])
            # ...and the bound itself is unchanged: 0s is fresh, 180s is fresh.
            for offset in (0, 180):
                ok = health.check_cron_runner(
                    workspace, "test-host", "codex", killed, now=mtime + offset
                )
                self.assertEqual(ok["status"], "warn", f"{offset}s: {ok['detail']}")


if __name__ == "__main__":
    unittest.main()
