#!/usr/bin/env python3
"""The queue probe's subject is "is anything picking work up". A pool follower
holds a task by RENAMING it, so argv-based holdings see nothing and the probe
called a working pool stuck."""
import importlib.util
import tempfile
import time
import unittest
import unittest.mock
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

    def test_a_pool_hold_that_never_moves_is_not_ok(self):
        """A hold is a RENAME, so a dead holder keeps the name forever. Nothing
        on main reclaims it, so 'the pool is working' would be permanent."""
        self._task("task-z.claimed-core-9.txt", age_s=6 * 3600)
        r = self.hc.check_task_queue()
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("claimed:core-9", r["detail"])
        self.assertNotIn("not stalled", r["detail"])

    def test_assigned_is_gated_the_same_as_claimed(self):
        """`reclaim_dead` is on the unmerged pool branch, so on this tree an
        old `.assigned-` is no more self-correcting than an old `.claimed-`."""
        self._task("task-y.assigned-core-8.txt", age_s=6 * 3600)
        r = self.hc.check_task_queue()
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("assigned:core-8", r["detail"])

    def test_only_the_stale_holds_are_named(self):
        """Positive control: a fresh hold alongside a stale one must not be
        swept into the warning, or the probe just reports the whole pool."""
        self._task("task-fresh.claimed-core-1.txt", age_s=60)
        self._task("task-stale.claimed-core-9.txt", age_s=6 * 3600)
        r = self.hc.check_task_queue()
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("1 of 2", r["detail"])
        self.assertIn("core-9", r["detail"])
        self.assertNotIn("core-1", r["detail"])

    def test_a_hold_that_vanishes_mid_sweep_is_skipped_not_fatal(self):
        """The queue moves while the probe reads it: a follower can finish and
        archive between the listing and the stat. Losing the race must cost that
        one file, not the whole verdict."""
        self._task("task-gone.claimed-core-9.txt", age_s=6 * 3600)
        self._task("task-stays.claimed-core-8.txt", age_s=6 * 3600)
        real_stat = Path.stat
        target = self.ws / "tasks" / "task-gone.claimed-core-9.txt"

        def flaky(self_p, *a, **kw):
            if self_p == target:
                raise OSError(2, "No such file or directory")
            return real_stat(self_p, *a, **kw)

        with unittest.mock.patch.object(Path, "stat", flaky):
            r = self.hc.check_task_queue()
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("core-8", r["detail"])
        self.assertNotIn("core-9", r["detail"])

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


class PoolHeldStuckVanishing(unittest.TestCase):
    """A pool file can be renamed or archived between listing and stat(), so the
    age filter must survive a stat() that raises rather than abandoning the scan."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()
        (self.ws / "results").mkdir()
        self.hc = _load(self.ws)
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def _aged(self, name, age_s):
        import os
        p = self.ws / "tasks" / name
        p.write_text("id: t\ntask: body\n")
        os.utime(p, (self.now - age_s, self.now - age_s))
        return p

    def test_only_files_past_the_age_are_stuck(self):
        old = self._aged("task-old.claimed-core-1.txt", 600)
        fresh = self._aged("task-new.claimed-core-1.txt", 5)
        out = self.hc._pool_held_stuck([old, fresh], self.now, 300)
        self.assertEqual(out, [old])

    def test_a_vanished_file_is_skipped_and_the_scan_continues(self):
        gone = self.ws / "tasks" / "task-gone.claimed-core-1.txt"
        old = self._aged("task-old.claimed-core-1.txt", 600)
        # `gone` first: a raise that aborted the loop would drop `old` silently.
        out = self.hc._pool_held_stuck([gone, old], self.now, 300)
        self.assertEqual(out, [old])


if __name__ == "__main__":
    unittest.main(verbosity=2)
