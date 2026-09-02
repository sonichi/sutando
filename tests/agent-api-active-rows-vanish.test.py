#!/usr/bin/env python3
"""A task file claimed mid-scan must not 500 the /tasks/active endpoint.

A claim RENAMES `task-{id}.txt` to `task-{id}.claimed-core-N.txt`. Between
`TASK_DIR.glob` listing the old name and `_active_task_rows` reading it, the
path is gone; the exception escapes `do_GET` and the client sees the connection
closed with no response.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent-api.py"


def _load():
    spec = importlib.util.spec_from_file_location("agent_api", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_api"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


class VanishingTaskFile(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        ws = Path(self.tmp.name)
        self.tasks = ws / "tasks"
        self.results = ws / "results"
        self.tasks.mkdir()
        self.results.mkdir()
        (self.results / "archive").mkdir()
        self.real = self.tasks / "task-1788000000000.txt"
        self.real.write_text("id: task-1788000000000\ntask: real one\nsource: chat\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self, ghost=False):
        """Returns (rows, snapshot of task_history taken INSIDE the patches).

        The snapshot matters: patch.dict restores on exit, so asserting after
        the with-block inspects the original empty dict, not the run's result.
        """
        real_glob = Path.glob

        def fake_glob(self_path, pattern):
            yield from real_glob(self_path, pattern)
            if ghost and self_path == self.tasks:
                yield self.tasks / "task-1788000000001.claimed-core-1.txt"

        with patch.object(self.mod, "TASK_DIR", self.tasks), \
             patch.object(self.mod, "RESULT_DIR", self.results), \
             patch.dict(self.mod.task_history, {}, clear=True), \
             patch.object(Path, "glob", fake_glob):
            rows = self.mod._active_task_rows()
            return rows, dict(self.mod.task_history)

    def test_a_claimed_away_file_does_not_raise(self):
        """The defect: read_text() on the renamed-away path escapes do_GET."""
        rows, _ = self._rows(ghost=True)
        self.assertIsInstance(rows, list)

    def test_the_surviving_task_still_appears(self):
        """Skipping the ghost must not drop the real row."""
        _, hist = self._rows(ghost=True)
        self.assertIn("task-1788000000000", hist)

    def test_control_no_ghost_behaves_normally(self):
        """Negative control: with every listed path present, unchanged."""
        rows, hist = self._rows(ghost=False)
        self.assertIsInstance(rows, list)
        self.assertIn("task-1788000000000", hist)


if __name__ == "__main__":
    unittest.main(verbosity=2)
