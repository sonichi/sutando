#!/usr/bin/env python3
"""Regression test: check_task_claim_age must make a LEAKED task-handler claim
loud while it is leaking, instead of only at watcher exit.

The gap it covers: `watch-tasks-stream.sh` takes a claim in
state/task-event-handler-claims/ before dispatching a task to
$SUTANDO_TASK_EVENT_HANDLER and releases it on completion. A claim that is
never released takes NO error path, so nothing is logged and every other probe
reads healthy. It surfaces only when the watcher exits, where
fallback_outstanding_handlers() publishes one user-visible terminal failure per
held claim — so a slow leak's first and only symptom is a flood at restart.

Measured 2026-08-14: 34 claims accumulated over 21h, oldest 31.2h, and drained
as 34 Discord messages in two seconds on restart. The retired watcher's entire
captured stderr was 228 lines — 194 task events plus those 34 shutdown lines,
and nothing else. Zero handler failures. Nothing could have reported the leak.

Run: python3 tests/health-check-task-claim-age.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_health_check():
    spec = importlib.util.spec_from_file_location(
        "health_check_claim_age_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestTaskClaimAge(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.claims = self.ws / "state" / "task-event-handler-claims"

    def tearDown(self):
        self._tmp.cleanup()

    def _claim(self, name: str, age_s: float) -> Path:
        self.claims.mkdir(parents=True, exist_ok=True)
        path = self.claims / name
        path.write_text("claim\nwatcher-id\n/tasks/x.txt\nmust-handle\n")
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    # --- the assertions that must FAIL in the broken state -------------------

    def test_leaked_claim_reports_down(self):
        """A 31h claim — the measured 2026-08-14 case — must read `down`."""
        self._claim("task-1786641305509.txt", 31.2 * 3600)
        out = self.hc.check_task_claim_age(workspace_dir=self.ws)
        self.assertEqual(out["status"], "down")
        self.assertIn("31.2h", out["detail"])

    def test_aging_claim_reports_warn(self):
        """Past any bounded handler run (codex-bounded caps at 240s) but not yet
        `down`: the window where the leak is still cheap to catch."""
        self._claim("task-1786641305509.txt", 45 * 60)
        self.assertEqual(
            self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "warn"
        )

    def test_oldest_claim_decides_not_the_count(self):
        """Many fresh claims must not average away one old one — a busy handler
        would otherwise mask the leak exactly when it is worst."""
        for i in range(12):
            self._claim(f"task-fresh-{i}.txt", 5)
        self._claim("task-old.txt", 9 * 3600)
        out = self.hc.check_task_claim_age(workspace_dir=self.ws)
        self.assertEqual(out["status"], "down")
        self.assertIn("13 held claim(s)", out["detail"])
        self.assertIn("task-old.txt", out["detail"])

    # --- and the clean states, so the probe cannot be trivially always-down ---

    def test_in_flight_claim_is_ok(self):
        """A claim younger than one handler run is normal operation."""
        self._claim("task-1786641305509.txt", 30)
        self.assertEqual(
            self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "ok"
        )

    def test_empty_claims_dir_is_ok(self):
        self.claims.mkdir(parents=True, exist_ok=True)
        self.assertEqual(
            self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "ok"
        )

    def test_absent_claims_dir_is_ok(self):
        """A host that never dispatched to the handler has no directory, and
        that is not a fault — an absent-is-warn probe warns forever and gets
        ignored, which is how the alarm this exists to raise would be lost."""
        self.assertEqual(
            self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "ok"
        )

    def test_non_claim_files_are_ignored(self):
        """Release/retire use `.stale-*` and `.claim-*` temporaries in the same
        directory; only published `task-*.txt` claims are held work."""
        self.claims.mkdir(parents=True, exist_ok=True)
        stale = self.claims / ".stale-watcher-task-1.txt"
        stale.write_text("x")
        old = time.time() - 40 * 3600
        os.utime(stale, (old, old))
        self.assertEqual(
            self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "ok"
        )

    def test_probe_is_registered_in_the_report(self):
        """A probe that exists but is never appended to `checks` cannot fail."""
        source = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_task_claim_age())", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
