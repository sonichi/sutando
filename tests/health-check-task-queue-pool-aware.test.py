#!/usr/bin/env python3
"""The queue probe's subject is "is anything picking work up". A pool follower
holds a task by RENAMING it, so argv-based holdings see nothing and the probe
called a working pool stuck."""
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(ws: Path):
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    m.WORKSPACE_DIR = ws
    m._worker_holdings = lambda ps_output=None: {}   # no session-worker anywhere
    return m


class PoolHeldQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()
        (self.ws / "results").mkdir()
        self.hc = _load(self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, name, age_s=0):
        p = self.ws / "tasks" / name
        p.write_text("id: t\ntask: body\n")
        if age_s:
            old = time.time() - age_s
            import os
            os.utime(p, (old, old))
        return p

    def test_a_working_pool_is_not_reported_stuck(self):
        """4 old tasks, every one held by a follower — the exact live shape."""
        for i, n in enumerate(["a.assigned-core-1", "b.claimed-core-2",
                               "c.assigned-core-3", "d.claimed-core-4"]):
            self._task(f"task-{n}.txt", age_s=600 + i)
        r = self.hc.check_task_queue()
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertIn("0 unassigned", r["detail"])
        self.assertNotIn("may be stuck", r["detail"])

    def test_a_real_pileup_still_warns(self):
        """The guard must not disarm the probe: unassigned work still fires."""
        for i in range(4):
            self._task(f"task-plain{i}.txt", age_s=600 + i)
        r = self.hc.check_task_queue()
        self.assertEqual(r["status"], "warn", r["detail"])

    def test_unassigned_backlog_warns_even_beside_pool_work(self):
        """A follower being busy must not mask work nothing has picked up."""
        for i in range(4):
            self._task(f"task-plain{i}.txt", age_s=600 + i)
        self._task("task-held.claimed-core-4.txt", age_s=900)
        r = self.hc.check_task_queue()
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("unassigned", r["detail"])
        self.assertIn("held by pool followers", r["detail"])

    def test_the_detail_names_who_holds_what(self):
        self._task("task-x.claimed-core-2.txt", age_s=600)
        self._task("task-y.assigned-core-3.txt", age_s=600)
        d = self.hc.check_task_queue()["detail"]
        self.assertIn("claimed:core-2", d)
        self.assertIn("assigned:core-3", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
