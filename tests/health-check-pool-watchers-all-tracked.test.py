#!/usr/bin/env python3
"""On a pool host the task-watcher probe must consider EVERY sentinel.

Each watcher writes its own `state/watch-tasks-stream[-<instance>].pid`, so a
probe that reads one file reports on one watcher and classifies the other N-1
live, correctly-supervised watchers as untracked duplicates. A peer's proactive
loop acts on that tracked/untracked split, so the false verdict is not cosmetic:
it prescribes stopping watchers that are doing their job.

The pre-fix reader took `watcher_sentinel_paths(...)[0]`, which is the historic
name whenever one exists and otherwise the alphabetically-first instance --
never a recency choice, despite the comment that said so.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("hc", ROOT / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
sys.modules["hc"] = hc
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass

WATCHER_ARGV = "bash src/watch-tasks-stream.sh"


def run(sentinels: dict, trees: dict, argv=WATCHER_ARGV, core_alive=True) -> dict:
    """`sentinels` maps filename -> contents; `trees` maps root pid -> members."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "state" / "cores").mkdir(parents=True)
        if core_alive:
            (ws / "state" / "cores" / "h.alive").write_text("{}")
        for fn, text in sentinels.items():
            (ws / "state" / fn).write_text(text)
        saved = (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees,
                 hc._ps_snapshot, hc._pid_parent, hc._fresh_local_core_record)
        try:
            hc.WORKSPACE_DIR = ws
            hc._proc_argv = (argv if callable(argv) else (lambda pid: argv))
            hc._watcher_trees = lambda *a, **k: trees
            hc._ps_snapshot = lambda *a, **k: ""
            hc._pid_parent = lambda pid, ps=None: "1"
            hc._fresh_local_core_record = lambda *a, **k: ({} if core_alive else None)
            return hc.check_task_watcher()
        finally:
            (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees,
             hc._ps_snapshot, hc._pid_parent, hc._fresh_local_core_record) = saved


class PoolHost(unittest.TestCase):
    def test_every_instance_watcher_is_tracked(self):
        """THE case: three sentinels, three live watchers, nothing untracked.

        Pre-fix this warned that 2 of 3 were untracked duplicates and told the
        operator to stop them.
        """
        r = run({"watch-tasks-stream.pid": "100\n",
                 "watch-tasks-stream-worker-1.pid": "200\n",
                 "watch-tasks-stream-worker-2.pid": "300\n"},
                {"100": {"100"}, "200": {"200"}, "300": {"300"}})
        self.assertEqual(r["status"], "ok", r["detail"])
        for pid in ("100", "200", "300"):
            self.assertIn(pid, r["detail"])

    def test_a_genuinely_untracked_watcher_is_still_reported(self):
        """The union must not swallow the defect the probe exists to find."""
        r = run({"watch-tasks-stream.pid": "100\n",
                 "watch-tasks-stream-worker-1.pid": "200\n"},
                {"100": {"100"}, "200": {"200"}, "999": {"999"}})
        self.assertEqual(r["status"], "warn")
        self.assertIn("999", r["detail"])
        self.assertNotIn("stop them and restart one cleanly", r["detail"])

    def test_only_the_untracked_root_is_named(self):
        r = run({"watch-tasks-stream-worker-1.pid": "200\n"},
                {"200": {"200"}, "888": {"888"}})
        self.assertEqual(r["status"], "warn")
        self.assertIn("888", r["detail"])
        self.assertIn("Keep the tracked one(s) (200)", r["detail"])

    def test_a_dead_sentinel_beside_a_live_one_does_not_warn(self):
        """A worker that exited leaves its file; the rest are still correct."""
        argv = lambda pid: "" if str(pid) == "100" else WATCHER_ARGV  # noqa: E731
        r = run({"watch-tasks-stream.pid": "100\n",
                 "watch-tasks-stream-worker-1.pid": "200\n"},
                {"200": {"200"}}, argv=argv)
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertIn("200", r["detail"])

    def test_all_sentinels_dead_with_watchers_running_keeps_the_old_verdict(self):
        r = run({"watch-tasks-stream.pid": "100\n"}, {"777": {"777"}}, argv="")
        self.assertEqual(r["status"], "warn")
        self.assertIn("orphaned", r["detail"])

    def test_a_tree_whose_member_is_tracked_counts_as_tracked(self):
        """A watcher's tree holds its children; the sentinel names the root."""
        r = run({"watch-tasks-stream.pid": "100\n"}, {"100": {"100", "101", "102"}})
        self.assertEqual(r["status"], "ok", r["detail"])


class SingleInstanceUnchanged(unittest.TestCase):
    """Every historic branch must read the same on a one-watcher host."""

    def test_ok(self):
        r = run({"watch-tasks-stream.pid": "100\n"}, {"100": {"100"}})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["detail"], "streaming watcher alive (pid 100)")

    def test_pid_reuse(self):
        r = run({"watch-tasks-stream.pid": "100\n"}, {}, argv="/usr/bin/python3 other.py")
        self.assertEqual(r["status"], "warn")
        self.assertIn("PID reuse", r["detail"])

    def test_unreadable(self):
        r = run({"watch-tasks-stream.pid": "not-a-pid\n"}, {})
        self.assertEqual(r["status"], "warn")
        self.assertIn("unreadable PID sentinel", r["detail"])

    def test_dead_with_nothing_running(self):
        r = run({"watch-tasks-stream.pid": "100\n"}, {}, argv="")
        self.assertEqual(r["status"], "warn")
        self.assertIn("is dead", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
