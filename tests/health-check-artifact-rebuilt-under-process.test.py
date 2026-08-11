#!/usr/bin/env python3
"""
Tests for the ARTIFACT-VS-PROCESS comparison in `mark_stale_if_outdated`
(`src/health-check.py`) — "the binary was rebuilt after the process that
executes it started, so the running process is not the deployed code."

The helper already had two comparisons, and both use source mtime as the
reference point:

    src_mtime - bin_mtime   > threshold  -> "rebuild needed"
    src_mtime - proc_start  > threshold  -> "restart needed"

`bin_mtime - proc_start` did not exist. For a compiled/bundled service
that is what actually matters: the process executes the ARTIFACT, so a
deploy that refreshes the artifact without touching source is invisible
to both comparisons — the first yields a negative (binary NEWER than
source) and the second never fires (source untouched). "Deployed" reads
as "running".

Observed 2026-08-11 on this host: `dist/credential-proxy.js` was
refreshed at 11:30:38 under a proxy process that had started ~11:28.
The source `.ts` was untouched from an earlier day, so health-check
reported the service healthy while it was executing the pre-deploy
artifact.

Cases:
  a) artifact rebuilt AFTER the process started    -> stale, "restart needed"
  b) artifact rebuilt BEFORE the process started
     (the normal deploy-then-restart order)        -> ok
  c) rebuild lands within `artifact_threshold_sec`
     of the start (build finishing just after a
     launch)                                       -> ok
  d) `binary_path` is None (the tsx callers)       -> comparison skipped
  e) the artifact vanishes between `exists()` and
     `stat()` (an atomic deploy renaming over it)  -> no crash, falls
                                                     through to the
                                                     source comparison

(a) is the new behavior: it FAILS on the parent commit, where the branch
does not exist. (b)-(d) are the guard rails that keep it from being a
blanket flag.

Sibling suite: `tests/health-check-compiled-artifact-stale.test.py`
covers the OTHER compiled-artifact comparison (binary older than source
-> "rebuild needed") and its git content cross-check. That suite stubs
pgrep to return no PIDs, so it short-circuits before the code under test
here; this file stubs pgrep and `ps` to present a live process instead.

Run: python3 tests/health-check-artifact-rebuilt-under-process.test.py
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

# Captured before any patching: the tests stub `hc.subprocess.run`, which is
# the same module object this file imported, so the stub needs an unpatched
# handle to fall through to.
_REAL_RUN = subprocess.run

THRESHOLD = 1800        # mark_stale_if_outdated's default threshold_sec
ARTIFACT_THRESHOLD = 120  # its default artifact_threshold_sec
FAKE_PID = "424242"


def _set_mtime(p: Path, ts: float) -> None:
    os.utime(p, (ts, ts))


def _lstart(ts: float) -> str:
    """Format `ts` the way `ps -o lstart=` prints it on macOS/BSD."""
    return datetime.fromtimestamp(ts).strftime("%a %b %d %H:%M:%S %Y")


class ArtifactRebuiltUnderProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self._patch = patch.object(hc, "REPO_DIR", self.repo)
        self._patch.start()
        self.addCleanup(self._patch.stop)

        self.now = time.time()
        # Source is old and untouched — that is the whole point. If source
        # were newer than proc_start the pre-existing comparison would flag
        # anyway and these cases would not isolate the artifact check.
        self.src = self.repo / "src" / "proxy.ts"
        self.src.parent.mkdir(parents=True, exist_ok=True)
        self.src.write_text("export const x = 1\n")
        _set_mtime(self.src, self.now - 30 * 3600)

        self.binary = self.repo / "dist" / "proxy.js"
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        self.binary.write_text("compiled\n")

        self.proc_start = self.now - 2 * 3600

    def _fake_run(self, cmd, *args, **kwargs):
        """Present one live PID that belongs to this checkout.

        `_filter_pids_this_checkout` probes `ps -o command=` first and keeps
        a PID whose argv contains `<REPO_DIR>/`, so the argv answer must be
        repo-rooted or the PID is dropped and the whole branch is skipped.
        """
        joined = " ".join(str(c) for c in cmd)
        if "pgrep" in str(cmd[0]):
            return subprocess.CompletedProcess(cmd, 0, stdout=FAKE_PID + "\n", stderr="")
        if "command=" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="node {}/dist/proxy.js\n".format(self.repo), stderr="")
        if "lstart=" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_lstart(self.proc_start) + "\n", stderr="")
        return _REAL_RUN(cmd, *args, **kwargs)

    def _run(self, binary_path=None) -> dict:
        check = {"name": "proxy", "status": "ok", "detail": "running"}
        with patch.object(hc.subprocess, "run", side_effect=self._fake_run):
            hc.mark_stale_if_outdated(check, self.src, "proxy-pattern",
                                      threshold_sec=THRESHOLD,
                                      binary_path=binary_path)
        return check

    # (a) THE FIX: artifact rebuilt while the process was already running.
    def test_artifact_rebuilt_after_process_start_is_stale(self):
        _set_mtime(self.binary, self.proc_start + 3600)
        check = self._run(binary_path=self.binary)
        self.assertEqual(check["status"], "stale")
        self.assertIn("rebuilt", check["detail"])
        self.assertIn("restart needed", check["detail"])
        self.assertIn("60 min", check["detail"])

    # (b) normal order — deploy, then restart. Nothing to report.
    def test_process_started_after_rebuild_is_ok(self):
        _set_mtime(self.binary, self.proc_start - 3600)
        check = self._run(binary_path=self.binary)
        self.assertEqual(check["status"], "ok")

    # (c) a build finishing seconds after launch is a race, not a stale deploy.
    def test_rebuild_within_grace_window_is_ok(self):
        _set_mtime(self.binary, self.proc_start + ARTIFACT_THRESHOLD - 30)
        check = self._run(binary_path=self.binary)
        self.assertEqual(check["status"], "ok")

    # (d) the tsx callers pass no binary — they must be untouched by this.
    def test_no_binary_path_skips_the_artifact_check(self):
        _set_mtime(self.binary, self.proc_start + 3600)
        check = self._run(binary_path=None)
        self.assertEqual(check["status"], "ok")

    # (e) the artifact disappears between the artifact block's own
    # `exists()` and its `stat()` — what an atomic deploy (write tmp, rename
    # over the target) does to a concurrent reader.
    #
    # `binary_path` is stat()ed four times per call, and only the LAST is
    # inside the branch under test:
    #   1. exists()  -- pre-existing "binary older than source" block
    #   2. stat()    -- same block
    #   3. exists()  -- the artifact-vs-process block
    #   4. stat()    -- the artifact-vs-process block  <- the guarded call
    # Raising earlier does not exercise the guard: a raise on 3 is caught by
    # `Path.exists()` itself, which returns False and skips the branch whole.
    # The exact-count assertion is deliberate — if that sequence ever changes
    # this test fails loudly instead of silently covering nothing.
    #
    # Source is made NEWER than the process here, which is what makes the
    # guard observable at all. The function's outer handler also catches
    # OSError, so without the inner `except` the error unwinds past the
    # SOURCE comparison too and the service reads "ok". The inner guard
    # confines the failure to the artifact check and lets the source
    # comparison still return its verdict — so "stale" vs "ok" is exactly
    # the difference the guard makes, and deleting it fails this test.
    def test_artifact_vanishing_between_exists_and_stat_is_confined(self):
        _set_mtime(self.binary, self.proc_start + 3600)
        _set_mtime(self.src, self.proc_start + 3600)
        real_stat = Path.stat
        seen = {"n": 0}

        def _racing_stat(inner_self, *args, **kwargs):
            if inner_self == self.binary:
                seen["n"] += 1
                if seen["n"] == 4:
                    raise OSError(2, "No such file or directory")
            return real_stat(inner_self, *args, **kwargs)

        with patch.object(Path, "stat", _racing_stat):
            check = self._run(binary_path=self.binary)

        self.assertEqual(seen["n"], 4)
        # The source comparison still ran and still decided.
        self.assertEqual(check["status"], "stale")
        self.assertIn("newer than process", check["detail"])
        # ...and it is the SOURCE verdict, not the artifact one, which could
        # not have been reached.
        self.assertNotIn("rebuilt", check["detail"])


if __name__ == "__main__":
    result = unittest.main(argv=[sys.argv[0], "-v"], exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
