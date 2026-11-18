#!/usr/bin/env python3
"""The cron-schedule probe: a registered schedule that stopped firing.

`session-crons` checks that registration HAPPENED; nothing checks that it is
still happening. CronCreate jobs expire after 7 days, so a host can hold an
honest registration stamp and a dead schedule at the same time.

Run: python3 tests/health-check-cron-schedule-liveness.test.py
"""

from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class CronScheduleLivenessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        (self.ws / "state").mkdir(parents=True)
        self._host = unittest.mock.patch.object(hc, "_host_label", return_value="test-host")
        self._host.start()
        self.addCleanup(self._host.stop)
        self._orig = hc.WORKSPACE_DIR
        hc.WORKSPACE_DIR = self.ws

    def tearDown(self) -> None:
        hc.WORKSPACE_DIR = self._orig
        self._tmp.cleanup()

    def _stamper(self, age_h, stamps=True):
        d = self.ws / "fake_repo"
        (d / "scripts").mkdir(parents=True, exist_ok=True)
        f = d / "scripts" / "core-status.sh"
        f.write_text("--loop-complete\n" if stamps else "no completion writer\n")
        if age_h is not None:
            t = time.time() - age_h * 3600
            os.utime(f, (t, t))
        return unittest.mock.patch.object(hc, "REPO_DIR", d)

    def _mark(self, age_h, host="test-host"):
        from loop_completion import write_completion
        write_completion(self.ws, host, now=time.time() - age_h * 3600)

    def test_a_recent_pass_is_ok(self):
        self._mark(0.2)
        self.assertEqual(hc.check_cron_schedule()["status"], "ok")

    def test_a_schedule_quiet_past_the_warn_band_warns(self):
        self._mark(9)
        r = hc.check_cron_schedule()
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("9.0h", r["detail"])

    def test_a_schedule_quiet_past_the_fail_band_fails(self):
        self._mark(101)
        r = hc.check_cron_schedule()
        self.assertEqual(r["status"], "fail", r)
        self.assertIn("101.0h", r["detail"])

    def test_owner_status_and_legacy_marker_cannot_mask_dead_loop(self):
        self._mark(101)
        (self.ws / "state/core-status.json").write_text(json.dumps({"status": "idle", "ts": time.time()}))
        (self.ws / "state/last-loop-ok").touch()
        self.assertEqual(hc.check_cron_schedule()["status"], "fail")

    def test_first_proof_grace_is_bounded_by_writer_installation(self):
        with self._stamper(0.2):
            r = hc.check_cron_schedule()
            self.assertEqual(r["status"], "ok", r)
            self.assertIn("awaiting first", r["detail"])
        with self._stamper(8):
            self.assertEqual(hc.check_cron_schedule()["status"], "warn")
            (self.ws / "state/core-status.json").write_text("{}")
            (self.ws / "state/last-loop-ok").touch()
            self.assertEqual(hc.check_cron_schedule()["status"], "warn")

    def test_unavailable_writer_is_not_success(self):
        with self._stamper(400, stamps=False):
            self.assertEqual(hc.check_cron_schedule()["status"], "warn")

    def test_peer_completion_cannot_heal_missing_or_stale_local_proof(self):
        self._mark(0.1, host="peer-host")
        with self._stamper(400):
            self.assertEqual(hc.check_cron_schedule()["status"], "warn")
        self._mark(101)
        self.assertEqual(hc.check_cron_schedule()["status"], "fail")

    def test_invalid_and_future_records_are_never_healthy(self):
        from loop_completion import completion_path
        target = completion_path(self.ws, "test-host")
        target.parent.mkdir(parents=True)
        for value in ("{", "[]", json.dumps({"version": 1, "host": "peer-host", "completed_at": time.time()}),
                      json.dumps({"version": 1, "host": "test-host", "completed_at": time.time() + 3600})):
            with self.subTest(value=value):
                target.write_text(value)
                self.assertEqual(hc.check_cron_schedule()["status"], "warn")

    def test_only_explicit_parking_exempts_liveness(self):
        target = self.ws / "hosts/test-host/crons.json"
        target.parent.mkdir(parents=True)
        entry = {"name": "main-loop", "prompt_skill": "proactive-loop", "cron": "0 0 31 2 *"}
        self._mark(101)
        target.write_text(json.dumps([entry]))
        self.assertEqual(hc.check_cron_schedule()["status"], "fail")
        entry["disabled"] = True
        target.write_text(json.dumps([entry]))
        self.assertEqual(hc.check_cron_schedule()["status"], "ok")
        entry["cron"] = "*/5 * * * *"
        target.write_text(json.dumps([entry]))
        self.assertEqual(hc.check_cron_schedule()["status"], "fail")

    def test_autonomy_pause_does_not_exempt_health_pass(self):
        self._mark(101)
        (self.ws / "state/loop-paused-until.sentinel").write_text(str(time.time() + 3600))
        self.assertEqual(hc.check_cron_schedule()["status"], "fail")

    # --- band overrides ---------------------------------------------------

    def test_the_bands_are_env_tunable(self) -> None:
        """A host with a sparse loop must be able to widen the warn band."""
        self._mark(9)
        with unittest.mock.patch.dict(os.environ, {"SUTANDO_CRON_STALE_WARN_H": "12"}):
            self.assertEqual(hc.check_cron_schedule()["status"], "ok")

    def test_an_unparseable_band_falls_back_to_the_default(self) -> None:
        """A typo in the env must not silence the probe, nor crash the run."""
        self._mark(9)
        with unittest.mock.patch.dict(os.environ, {"SUTANDO_CRON_STALE_WARN_H": "soon"}):
            self.assertEqual(hc.check_cron_schedule()["status"], "warn")

    # --- the writer half --------------------------------------------------

    def _minimal_repo(self) -> Path:
        """A throwaway checkout whose config points `core-status.sh` at self.ws.

        `$SUTANDO_WORKSPACE` cannot do this: v0.8 (#1440) removed it from the
        resolution order, so setting it leaves the write landing in the REAL
        workspace while the test reads an empty temp dir. The supported
        override is `sutando.config.local.json`, and `_find_repo_root` anchors
        on the TRACKED `sutando.config.json`, so both files must be present.
        """
        self._repo_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._repo_tmp.cleanup)
        repo = Path(self._repo_tmp.name) / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "scripts").mkdir(parents=True)
        for rel in ("src/workspace_default.py", "src/sutando_config.py",
                    "src/loop_completion.py", "src/file_lock.py", "src/util_paths.py",
                    "scripts/sutando-config.sh", "scripts/python-binary.sh", "scripts/core-status.sh"):
            shutil.copy(REPO / rel, repo / rel)
        shutil.copy(REPO / "sutando.config.json", repo / "sutando.config.json")
        (repo / "sutando.config.local.json").write_text(
            json.dumps({"workspace": {"path": str(self.ws)}}))
        return repo

    def _core_status(self, repo: Path, *args: str) -> None:
        done = subprocess.run(["bash", str(repo / "scripts" / "core-status.sh"), *args],
                              cwd=repo, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0,
                         f"core-status.sh {args} failed: {done.stderr}")
        self.assertTrue(done.stdout.startswith(str(self.ws)),
                        f"wrote outside the temp workspace: {done.stdout!r}")


    def test_ordinary_core_status_idle_does_not_claim_loop_completion(self) -> None:
        """An owner conversation can finish while the proactive loop is absent."""
        repo = self._minimal_repo()
        marker = self.ws / "state" / "last-loop-ok"

        self._core_status(repo, "running", "x")
        running_stamped = marker.exists()

        self._core_status(repo, "idle")
        idle_stamped = marker.exists()

        self.assertFalse(running_stamped, "running must not stamp the pass-closed marker")
        self.assertFalse(idle_stamped, "ordinary idle must not claim a loop pass completed")

    def test_explicit_loop_completion_is_host_specific_and_idle_does_not_refresh_it(self) -> None:
        repo = self._minimal_repo()
        with unittest.mock.patch.dict(os.environ, {"SUTANDO_HOST_LABEL": "test-host"}):
            self._core_status(repo, "idle", "", "--loop-complete")
        marker = self.ws / "state" / "loop-completions" / "test-host.json"
        before = marker.read_bytes()
        self.assertEqual(json.loads(before)["host"], "test-host")
        self._core_status(repo, "idle")
        self.assertEqual(marker.read_bytes(), before)
        self.assertFalse((self.ws / "state" / "last-loop-ok").exists())

    # --- wiring -----------------------------------------------------------

    def test_probe_is_registered(self) -> None:
        """An unregistered probe is indistinguishable from green."""
        src = (REPO / "src" / "health-check.py").read_text()
        # Boolean first: assertIn on the whole file dumps 578KB into the report.
        self.assertTrue("checks.append(check_cron_schedule())" in src,
                        "check_cron_schedule is defined but never appended to checks")


if __name__ == "__main__":
    unittest.main(verbosity=2)
