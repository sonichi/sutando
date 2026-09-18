#!/usr/bin/env python3
"""Two health probes whose verdict named something they had not measured.

`check_task_watcher` returned "watcher not expected" after measuring CORE
HEARTBEATS, never watcher processes — and it read them fleet-wide, so another
machine's synced heartbeat decided whether THIS host should have a local
watcher. The green also short-circuited before enumeration, hiding the exact
orphaned-duplicate state the rest of that function exists to report.

`check_memory_index_integrity` says "compact it now" without naming which
MEMORY.md it measured. SUTANDO_MEMORY_DIR can point it at a corpus sessions
never load, and acting on that advice edits the wrong file.

Run: python3 tests/health-check-probe-names-its-subject.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class TaskWatcherSubjectTest(unittest.TestCase):
    """The 'not expected' green is a claim about this host's processes.

    check_task_watcher() takes the ps snapshot itself now, so every case that
    mocks the tree result must also pin the snapshot or it stops controlling
    its own premise on a runner where ps cannot run.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hc-watcher-"))
        self._ws = hc.WORKSPACE_DIR
        hc.WORKSPACE_DIR = self.tmp
        (self.tmp / "state" / "cores").mkdir(parents=True)

    def tearDown(self):
        hc.WORKSPACE_DIR = self._ws
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _heartbeat(self, host: str, *, fresh: bool = True):
        f = self.tmp / "state" / "cores" / f"{host}.alive"
        f.write_text(json.dumps({"ts": int(time.time())}))
        if not fresh:
            old = time.time() - 3600
            os.utime(f, (old, old))
        return f

    def test_idle_install_still_reports_ok(self):
        """No local core AND no watcher processes is a genuinely idle install.

        Preserved deliberately: a check that is always red carries the same
        information as one that is always green.
        """
        with patch.object(hc, "_ps_snapshot", return_value=""), \
             patch.object(hc, "_watcher_trees", return_value={}):
            out = hc.check_task_watcher()
        self.assertEqual(out["status"], "ok")
        self.assertIn("not expected", out["detail"])

    def test_running_watchers_are_never_called_not_expected(self):
        """THE BUG: no local heartbeat + watchers actually running.

        The old gate returned ok/"not expected" before enumerating, so two
        orphaned watchers double-processing every task were invisible on any
        host whose heartbeat had gone stale.
        """
        self._heartbeat(hc._host_label(), fresh=False)
        with patch.object(hc, "_ps_snapshot", return_value=""), \
             patch.object(hc, "_watcher_trees", return_value={"111": ["111"], "222": ["222"]}), \
             patch.object(hc, "_ps_snapshot", return_value=""), \
             patch.object(hc, "_pid_parent", return_value="1"):
            out = hc.check_task_watcher()
        self.assertNotEqual(out["status"], "ok",
                            f"visible watchers must outrank a stale heartbeat: {out!r}")
        self.assertNotIn("not expected", out["detail"])
        self.assertIn("111", out["detail"])
        self.assertIn("222", out["detail"])

    def test_ps_failure_is_unknown_not_an_empty_scan(self):
        """`_watcher_trees()` returns {} for a FAILED ps and a clean empty scan
        alike, so the idle green may only follow a scan that actually ran.

        Without this the probe re-commits the exact defect this PR is named for:
        asserting "no watcher processes" without having measured their absence.
        """
        with patch.object(hc, "_ps_snapshot", return_value=None):
            out = hc.check_task_watcher()
        self.assertNotEqual(out["status"], "ok",
                            f"a failed enumeration is UNKNOWN, not clear: {out!r}")
        self.assertNotIn("not expected", out["detail"])
        self.assertIn("ps unavailable", out["detail"])

    def test_a_nonzero_ps_exit_is_unavailable_not_an_empty_scan(self):
        """A command that FAILS but returns normally never raises, so its empty
        stdout would read as a scan that ran and found nothing.

        This is the same unmeasured-absence the probe fix closes, one layer
        down in the helper: `subprocess.run(...).stdout` is `""` for rc=1.
        """
        with patch.object(hc, "_platform_process_snapshot", return_value=""):
            self.assertEqual(hc._ps_snapshot(), "", "rc=0 + empty stdout IS a clean scan")
        with patch.object(hc, "_platform_process_snapshot", return_value=None):
            self.assertIsNone(hc._ps_snapshot(), "rc!=0 must be UNAVAILABLE, not empty")

    def test_a_failed_ps_that_returns_normally_still_warns(self):
        """End to end: the nonzero exit must reach the probe's verdict."""
        with patch.object(hc, "_platform_process_snapshot", return_value=None):
            out = hc.check_task_watcher()
        self.assertNotEqual(out["status"], "ok", f"false green on a failed ps: {out!r}")
        self.assertIn("ps unavailable", out["detail"])

    def test_an_empty_scan_that_ran_is_still_a_clean_result(self):
        """Mutation guard: ps returning NOTHING is not ps failing.

        Distinguishing them is the whole point; treating "" as unavailable would
        make the probe permanently warn on a genuinely idle install.
        """
        with patch.object(hc, "_ps_snapshot", return_value=""):
            out = hc.check_task_watcher()
        self.assertEqual(out["status"], "ok")
        self.assertIn("not expected", out["detail"])

    def test_a_peer_heartbeat_does_not_make_this_host_expect_a_watcher(self):
        """The watcher is a LOCAL process, so the gate must read the LOCAL core.

        `state/cores/*.alive` is synced across hosts, so the fleet-wide resolver
        let another machine's core decide this host's verdict.
        """
        self._heartbeat("some-peer-host", fresh=True)
        self.assertFalse((self.tmp / "state" / "cores" / f"{hc._host_label()}.alive").exists(),
                         "premise: only the PEER has a heartbeat")
        with patch.object(hc, "_ps_snapshot", return_value=""), \
             patch.object(hc, "_watcher_trees", return_value={}):
            out = hc.check_task_watcher()
        self.assertEqual(out["status"], "ok",
                         f"a peer's heartbeat must not make this host expect a watcher: {out!r}")
        self.assertIn("not expected", out["detail"])

    def test_live_local_core_with_no_watcher_still_warns(self):
        """Mutation guard: the real gap must survive the reordering."""
        self._heartbeat(hc._host_label(), fresh=True)
        with patch.object(hc, "_ps_snapshot", return_value=""), \
             patch.object(hc, "_watcher_trees", return_value={}):
            out = hc.check_task_watcher()
        self.assertEqual(out["status"], "warn")
        self.assertIn("not running", out["detail"])


class MemoryIndexSubjectTest(unittest.TestCase):
    """'compact it now' must say which MEMORY.md it means."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hc-memidx-"))
        self._md = hc.MEMORY_DIR

    def tearDown(self):
        hc.MEMORY_DIR = self._md
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _corpus(self, *, index_text: str, extra_files=()):
        d = self.tmp / "memory"
        d.mkdir(parents=True, exist_ok=True)
        (d / "MEMORY.md").write_text(index_text)
        for n in extra_files:
            (d / n).write_text("---\nname: x\n---\nbody\n")
        hc.MEMORY_DIR = d
        return d

    def test_detail_names_the_corpus_it_measured(self):
        d = self._corpus(index_text="# Index\n", extra_files=("orphan_memory.md",))
        with patch.object(hc, "_default_memory_dir", return_value=str(d)):
            out = hc.check_memory_index_integrity()
        self.assertIsNotNone(out)
        self.assertIn(str(d), out["detail"],
                      f"a destructive recommendation must name its target: {out['detail']!r}")

    def test_an_override_pointing_elsewhere_is_stated_outright(self):
        """The dangerous case: the measured corpus is NOT the workspace default.

        Then 'compact it now' is advice about a file sessions may never read.
        """
        d = self._corpus(index_text="# Index\n", extra_files=("orphan_memory.md",))
        other = self.tmp / "workspace-default-memory"
        other.mkdir(parents=True, exist_ok=True)
        with patch.object(hc, "_default_memory_dir", return_value=str(other)):
            out = hc.check_memory_index_integrity()
        self.assertIsNotNone(out)
        detail = out["detail"]
        self.assertIn("SUTANDO_MEMORY_DIR", detail)
        self.assertIn(str(other), detail,
                      f"must name the default it diverges from: {detail!r}")
        self.assertIn("never read", detail)

    def test_clean_index_also_names_its_corpus(self):
        """The ok line makes a claim too — about which corpus is healthy."""
        d = self._corpus(index_text="# Index\n")
        with patch.object(hc, "_default_memory_dir", return_value=str(d)):
            out = hc.check_memory_index_integrity()
        if out is not None:
            self.assertIn(str(d), out["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
