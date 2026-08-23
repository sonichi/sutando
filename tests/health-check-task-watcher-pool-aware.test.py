#!/usr/bin/env python3
"""The task-watcher probe must not tell a pool to kill its own watchers.

A pool runs one watcher per core. The PID sentinel is single-valued and is held
by whichever core stamped last, so N-1 of N watchers always read as "not tracked
by the sentinel". The pre-fix verdict ended "Keep the sentinel's (pid), stop the
rest", which on a 4-core host advises stopping 3 live watchers — every core but
one stops draining tasks/.

The single-core verdict is unchanged: there the extra tree really is a duplicate.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc", _REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


class LiveCoreInstances(unittest.TestCase):
    def setUp(self):
        self.hc = _load()

    def _ws(self, td, names, age_s=0.0):
        ws = Path(td)
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        for n in names:
            f = cores / f"{n}.alive"
            f.write_text("{}")
            if age_s:
                old = time.time() - age_s
                import os
                os.utime(f, (old, old))
        return ws

    def test_counts_every_fresh_core(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, ["core-1", "core-2", "core-3"])
            self.assertEqual(self.hc._live_core_instances(ws), {"core-1", "core-2", "core-3"})

    def test_stale_heartbeats_are_not_counted(self):
        """A pool that died must not keep suppressing the duplicate warning."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, ["core-1", "core-2"], age_s=600.0)
            self.assertEqual(self.hc._live_core_instances(ws), set())

    def test_missing_dir_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self.hc._live_core_instances(Path(td)), set())

    def test_agrees_with_any_core_alive(self):
        """Positive control: the two helpers must not disagree about liveness."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, ["core-1"])
            self.assertTrue(self.hc._any_core_alive(ws))
            self.assertTrue(self.hc._live_core_instances(ws))
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, ["core-1"], age_s=600.0)
            self.assertFalse(self.hc._any_core_alive(ws))
            self.assertFalse(self.hc._live_core_instances(ws))


class UnreadableHeartbeatIsSkipped(unittest.TestCase):
    def test_dangling_alive_symlink_is_not_counted_and_does_not_raise(self):
        """glob() yields the entry, stat() raises — the probe must skip it.

        A core removing its .alive between the glob and the stat is the live
        race; a dangling symlink reproduces it deterministically.
        """
        hc = _load()
        td = tempfile.mkdtemp()
        cores = Path(td) / "state" / "cores"
        cores.mkdir(parents=True)
        (cores / "core-1.alive").write_text("{}")
        (cores / "core-broken.alive").symlink_to(cores / "does-not-exist")
        self.assertEqual(hc._live_core_instances(Path(td)), {"core-1"})


class ProbeVerdictIsPoolAware(unittest.TestCase):
    """Drives the REAL check_task_watcher(), not the helper.

    The helper-only tests below pass even with the production branch disabled —
    a reviewer proved it with `if False and len(cores) > 1`. Only calling the
    function pins the decision this fix exists to make.
    """

    def _verdict(self, core_names):
        hc = _load()
        td = tempfile.mkdtemp()
        ws = Path(td)
        (ws / "state" / "cores").mkdir(parents=True)
        for n in core_names:
            (ws / "state" / "cores" / f"{n}.alive").write_text("{}")
        (ws / "state" / "watch-tasks-stream.pid").write_text("100")
        hc.WORKSPACE_DIR = ws
        # Only the OS-facing edges are stubbed; the decision under test is real.
        hc._watcher_trees = lambda ps_output=None: {
            "100": ["100"], "200": ["200"], "300": ["300"], "400": ["400"]}
        hc._proc_argv = lambda pid: "bash src/watch-tasks-stream.sh"
        hc._any_core_alive = lambda *a, **k: True
        return hc.check_task_watcher()["detail"]

    def test_pool_verdict_never_advises_stopping_the_extras(self):
        d = self._verdict(["core-1", "core-2", "core-3", "main"])
        self.assertNotIn("stop the rest", d,
                         "multi-core verdict must not advise killing live watchers")
        self.assertIn("DO NOT stop", d)
        self.assertIn("4 cores are live", d)

    def test_single_core_still_gets_the_duplicate_stop_advice(self):
        """Positive control: the original advice is correct when one core runs."""
        d = self._verdict(["main"])
        self.assertIn("stop the rest", d)
        self.assertNotIn("DO NOT stop", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
