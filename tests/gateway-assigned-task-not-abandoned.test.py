#!/usr/bin/env python3
"""A lead-assigned task must not be reconciled away as abandoned.

`_reconcile_abandoned` recognised the follower's `.claimed-<core>` rename but
not the lead's earlier `.assigned-<core>` rename. In the window between the two,
no `task-<id>.txt`, no `.claimed-*` and no result file existed, so the id looked
completed-elsewhere and was dropped from the in-flight ledger. `_post_ready_results`
only ever iterates in-flight ids, so the reply the follower wrote seconds later was
never posted and stranded in results/ with no dead-letter and no alert.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

TID = "task-9a153e2032e6cabe5a"


def _load_gateway():
    from ag2_sparrow import remote_gateway_bridge as gw  # noqa: WPS433
    return gw


class _Dirs:
    """Point the module's tasks/results dirs at a temp tree, then restore."""

    _PATCH = ("TASKS_DIR", "RESULTS_DIR")

    def __init__(self, gw, tmp: Path):
        self.gw, self.tmp = gw, tmp
        self._saved = {}

    def __enter__(self):
        for name in self._PATCH:
            self._saved[name] = getattr(self.gw, name)
        self.tasks = self.tmp / "tasks"
        self.results = self.tmp / "results"
        self.tasks.mkdir(parents=True)
        self.results.mkdir(parents=True)
        self.gw.TASKS_DIR = self.tasks
        self.gw.RESULTS_DIR = self.results
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(self.gw, name, value)
        return False


class AssignedTaskNotAbandoned(unittest.TestCase):
    def setUp(self):
        self.gw = _load_gateway()

    def _reconcile_twice(self, dirs):
        """Two consecutive sightings are what actually drops an id."""
        inflight = {TID}
        suspects = self.gw._reconcile_abandoned(inflight, set())
        self.gw._reconcile_abandoned(inflight, suspects)
        return inflight

    def test_assigned_task_survives_reconcile(self):
        with tempfile.TemporaryDirectory() as td, _Dirs(self.gw, Path(td)) as d:
            (d.tasks / f"{TID}.assigned-core-3.txt").write_text("task: hi\n")
            self.assertIn(TID, self._reconcile_twice(d),
                          "lead-assigned task was dropped as abandoned")

    def test_claimed_task_survives_reconcile(self):
        with tempfile.TemporaryDirectory() as td, _Dirs(self.gw, Path(td)) as d:
            (d.tasks / f"{TID}.claimed-core-3.txt").write_text("task: hi\n")
            self.assertIn(TID, self._reconcile_twice(d))

    def test_genuinely_gone_task_is_still_dropped(self):
        """The reconciler must keep doing its job — this is the positive control."""
        with tempfile.TemporaryDirectory() as td, _Dirs(self.gw, Path(td)) as d:
            self.assertNotIn(TID, self._reconcile_twice(d),
                             "reconciler no longer drops truly abandoned ids")

    def test_pending_predicate_covers_every_custody_state(self):
        with tempfile.TemporaryDirectory() as td, _Dirs(self.gw, Path(td)) as d:
            self.assertFalse(self.gw._task_pending_locally(TID))
            for name in (f"{TID}.txt", f"{TID}.assigned-core-2.txt",
                         f"{TID}.claimed-core-2.txt"):
                path = d.tasks / name
                path.write_text("task: hi\n")
                self.assertTrue(self.gw._task_pending_locally(TID), name)
                path.unlink()


if __name__ == "__main__":
    unittest.main()
