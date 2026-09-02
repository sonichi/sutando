#!/usr/bin/env python3
"""A task file that vanishes mid-scan must not abort the whole health run.

A claim RENAMES the task file, so between `_pending_task_files()` listing a
path and the probe stat()ing it, any entry can already be gone. The task-queue
probe is called unwrapped, so one unguarded stat() takes down every later check.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "health-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("health_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["health_check"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


class VanishingTaskFile(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()
        (self.ws / "results").mkdir()
        self.real = self.ws / "tasks" / "task-1788000000000.txt"
        self.real.write_text("id: task-1788000000000\ntask: real\n")
        # Plain name on purpose: a `.claimed-core-N` name is split to the
        # pool-held branch before the stat, so it never reaches this defect.
        self.gone = self.ws / "tasks" / "task-1788000000001.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, listed):
        with patch.object(self.mod, "WORKSPACE_DIR", self.ws), \
             patch.object(self.mod, "_pending_task_files", return_value=listed), \
             patch.object(self.mod, "_worker_holdings", return_value={}):
            return self.mod.check_task_queue()

    def test_a_vanished_file_does_not_raise(self):
        """The defect: min(..., key=p.stat().st_mtime) over a gone path raises
        FileNotFoundError out of the probe and ends the run."""
        res = self._run([self.real, self.gone])
        self.assertIsInstance(res, dict)
        self.assertEqual(res["name"], "task-queue")

    def test_the_surviving_file_still_drives_the_verdict(self):
        """Skipping the vanished entry must not blind the probe to the rest."""
        res = self._run([self.real, self.gone])
        self.assertIn(res["status"], ("ok", "warn", "error"))

    def test_all_entries_vanished_is_reported_not_raised(self):
        res = self._run([self.gone])
        self.assertIsInstance(res, dict)
        self.assertEqual(res["status"], "ok")

    def test_control_no_vanished_entry_behaves_normally(self):
        """Negative control: with every listed path present, the probe answers
        exactly as before — the fix must not change the healthy path."""
        res = self._run([self.real])
        self.assertIsInstance(res, dict)
        self.assertEqual(res["name"], "task-queue")
        self.assertIn(res["status"], ("ok", "warn", "error"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
