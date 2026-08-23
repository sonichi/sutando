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


if __name__ == "__main__":
    unittest.main(verbosity=2)
