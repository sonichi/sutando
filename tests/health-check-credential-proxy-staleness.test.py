#!/usr/bin/env python3
"""Tests for `check_credential_proxy` (`src/health-check.py`) — the live caller
of the artifact-vs-process comparison, which the sibling suite cannot pin.

The artifact is passed only when the running process executes it: a dev host
runs the `.ts` through tsx and may hold a build it never executes.

Cases:
  a) bundled proxy, artifact rebuilt after start -> stale, "restart needed"
  b) bundled proxy -> the artifact is what gets passed
  c) dev/tsx proxy -> binary_path is None
  d) proxy down    -> warn "not running (optional)", no staleness check
  e) `_process_executes_artifact` on a missing artifact / no live pid /
     pgrep failure

Run: python3 tests/health-check-credential-proxy-staleness.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = _load_module()
_REAL_RUN = subprocess.run
FAKE_PID = "424242"


def _set_mtime(p: Path, ts: float) -> None:
    os.utime(p, (ts, ts))


def _lstart(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %b %d %H:%M:%S %Y")


class CredentialProxyStalenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        patcher = patch.object(hc, "REPO_DIR", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.now = time.time()
        self.proc_start = self.now - 2 * 3600

        self.src = self.repo / "skills" / "quota-tracker" / "scripts" / "credential-proxy.ts"
        self.src.parent.mkdir(parents=True, exist_ok=True)
        self.src.write_text("export const proxy = 1\n")
        # Source untouched for a day: if it were newer than the process the
        # pre-existing source comparison would flag on its own and these
        # cases would not isolate the artifact path.
        _set_mtime(self.src, self.now - 30 * 3600)

        self.artifact = self.repo / "dist" / "credential-proxy.js"
        self.artifact.parent.mkdir(parents=True, exist_ok=True)
        self.artifact.write_text("// compiled\n")
        _set_mtime(self.artifact, self.proc_start + 3600)

        self.argv = "node {}/dist/credential-proxy.js".format(self.repo)

    # ---- subprocess stubs -------------------------------------------------
    def _fake_run(self, cmd, *args, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "pgrep" in str(cmd[0]):
            return subprocess.CompletedProcess(cmd, 0, stdout=self.pids, stderr="")
        if "args=" in joined or "command=" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.argv + "\n", stderr="")
        if "lstart=" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_lstart(self.proc_start) + "\n", stderr="")
        return _REAL_RUN(cmd, *args, **kwargs)

    def _check(self, port_status="ok", pids=FAKE_PID + "\n"):
        self.pids = pids
        port_check = {"name": "credential-proxy", "status": port_status,
                      "detail": "listening" if port_status == "ok" else "not listening"}
        with patch.object(hc, "check_port", return_value=port_check), \
             patch.object(hc.subprocess, "run", side_effect=self._fake_run):
            return hc.check_credential_proxy()

    # ---- (a) the incident, end to end -------------------------------------
    def test_artifact_rebuilt_under_running_proxy_is_stale(self):
        check = self._check()
        self.assertEqual(check["status"], "stale")
        self.assertIn("rebuilt", check["detail"])
        self.assertIn("restart needed", check["detail"])

    def test_artifact_older_than_process_is_ok(self):
        _set_mtime(self.artifact, self.proc_start - 3600)
        check = self._check()
        self.assertEqual(check["status"], "ok")

    # ---- (b)/(c) what the call site passes ---------------------------------
    def test_bundled_proxy_passes_the_artifact(self):
        with patch.object(hc, "mark_stale_if_outdated") as marker:
            self._check()
        self.assertEqual(marker.call_count, 1)
        args, kwargs = marker.call_args
        self.assertEqual(kwargs["binary_path"], self.artifact)
        self.assertEqual(args[1], self.src)

    def test_dev_tsx_proxy_passes_no_artifact(self):
        self.argv = "node .../tsx {}/skills/quota-tracker/scripts/credential-proxy.ts".format(self.repo)
        with patch.object(hc, "mark_stale_if_outdated") as marker:
            self._check()
        self.assertIsNone(marker.call_args.kwargs["binary_path"])

    # ---- (d) down is still an optional-service warning ---------------------
    def test_down_is_downgraded_and_skips_staleness(self):
        with patch.object(hc, "mark_stale_if_outdated") as marker:
            check = self._check(port_status="down")
        self.assertEqual(check["status"], "warn")
        self.assertEqual(check["detail"], "not running (optional)")
        marker.assert_not_called()

    # ---- (e) the gate itself ----------------------------------------------
    def test_missing_artifact_is_not_executed(self):
        self.artifact.unlink()
        self.pids = FAKE_PID + "\n"
        with patch.object(hc.subprocess, "run", side_effect=self._fake_run):
            self.assertFalse(hc._process_executes_artifact(self.artifact, "credential-proxy"))

    def test_no_live_process_is_not_executed(self):
        self.pids = "\n"
        with patch.object(hc.subprocess, "run", side_effect=self._fake_run):
            self.assertFalse(hc._process_executes_artifact(self.artifact, "credential-proxy"))

    def test_live_process_executing_the_artifact_is_detected(self):
        self.pids = FAKE_PID + "\n"
        with patch.object(hc.subprocess, "run", side_effect=self._fake_run):
            self.assertTrue(hc._process_executes_artifact(self.artifact, "credential-proxy"))

    def test_pgrep_failure_is_not_executed(self):
        def boom(cmd, *a, **k):
            raise OSError("pgrep unavailable")
        with patch.object(hc.subprocess, "run", side_effect=boom):
            self.assertFalse(hc._process_executes_artifact(self.artifact, "credential-proxy"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
