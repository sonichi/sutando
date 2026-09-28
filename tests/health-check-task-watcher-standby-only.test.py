#!/usr/bin/env python3
"""The task-watcher probe must tell the supervisor's standby from the session watcher.

Both roles stamp the same per-instance sentinel (src/watch-tasks-stream.sh), so a
pool host whose worker ended its turn logged out ("Login expired · Please run
/login", its Monitor never re-armed) has a live sentinel naming the STANDBY the
notifier supervisor armed. Pre-fix the probe proved the pid alive and its script
the watcher, and reported "streaming watcher alive" for a seat that could do no
work (user feedback, 2026-09-29). Liveness was checked; capability was not.
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

WID = "7c54b230a8d94ea9b86f52d70134ac68"
INBOX = "/ws/state/workers/" + WID + "/deliveries"
SCRIPT = "src/watch-tasks-stream.sh"


def vector(role=None, inbox=INBOX):
    vec = ["bash", SCRIPT, inbox]
    if role:
        vec += ["--role", role]
    if inbox:
        vec += ["--inbox", inbox]
    return vec


def run(sentinels: dict, vectors: dict, unreadable=()) -> dict:
    """`sentinels` maps filename -> pid; `vectors` maps pid -> argv list, the
    OS-authoritative vector; a pid in `unreadable` has a flat argv only."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "state" / "cores").mkdir(parents=True)
        (ws / "state" / "cores" / "h.alive").write_text("{}")
        for fn, pid in sentinels.items():
            (ws / "state" / fn).write_text(str(pid))
        table = {str(p): v for p, v in vectors.items()}
        saved = (hc.WORKSPACE_DIR, hc._proc_argv, hc._proc_argv_vector, hc._watcher_trees,
                 hc._ps_snapshot, hc._pid_parent, hc._fresh_local_core_record,
                 hc._is_watcher_argv)
        try:
            hc.WORKSPACE_DIR = ws
            hc._proc_argv = lambda pid: " ".join(table.get(str(pid)) or vector())
            hc._proc_argv_vector = lambda pid: None if str(pid) in map(str, unreadable) else table.get(str(pid))
            hc._is_watcher_argv = lambda a, pid=None: True
            hc._watcher_trees = lambda *a, **k: {str(p): {str(p)} for p in sentinels.values()}
            hc._ps_snapshot = lambda *a, **k: ""
            hc._pid_parent = lambda pid, ps=None: "1"
            hc._fresh_local_core_record = lambda *a, **k: {}
            return hc.check_task_watcher()
        finally:
            (hc.WORKSPACE_DIR, hc._proc_argv, hc._proc_argv_vector, hc._watcher_trees,
             hc._ps_snapshot, hc._pid_parent, hc._fresh_local_core_record,
             hc._is_watcher_argv) = saved


class StandbyOnly(unittest.TestCase):
    def test_a_sentinel_naming_the_standby_warns_and_says_what_to_do(self):
        """THE case: the worker's only watcher is the standby the supervisor armed."""
        out = run({f"watch-tasks-stream-{WID}.pid": 4242}, {4242: vector("standby")})
        self.assertEqual(out["status"], "warn", out)
        self.assertIn("STANDBY", out["detail"])
        self.assertIn(f"watch-tasks-stream-{WID}.pid -> pid 4242", out["detail"])
        self.assertIn(INBOX, out["detail"])
        self.assertIn("/login", out["detail"])
        self.assertIn("--role session --inbox", out["detail"])

    def test_the_session_watcher_is_still_ok(self):
        out = run({f"watch-tasks-stream-{WID}.pid": 4242}, {4242: vector("session")})
        self.assertEqual(out["status"], "ok", out)

    def test_an_untagged_legacy_watcher_is_still_ok(self):
        out = run({"watch-tasks-stream.pid": 4242}, {4242: ["bash", SCRIPT]})
        self.assertEqual(out["status"], "ok", out)

    def test_a_healthy_core_beside_a_standby_only_worker_names_only_the_worker(self):
        out = run({"watch-tasks-stream.pid": 100, f"watch-tasks-stream-{WID}.pid": 4242},
                  {100: vector("session", "/ws/tasks"), 4242: vector("standby")})
        self.assertEqual(out["status"], "warn", out)
        self.assertIn("1 sentinel(s) name only the STANDBY", out["detail"])
        self.assertNotIn("pid 100", out["detail"])

    def test_an_unreadable_operand_vector_proves_no_role_so_it_is_not_invented(self):
        out = run({f"watch-tasks-stream-{WID}.pid": 4242}, {4242: vector("standby")},
                  unreadable=(4242,))
        self.assertEqual(out["status"], "ok", out)

    def test_a_standby_that_states_no_inbox_is_still_named(self):
        out = run({f"watch-tasks-stream-{WID}.pid": 4242},
                  {4242: ["bash", SCRIPT, "--role", "standby"]})
        self.assertEqual(out["status"], "warn", out)
        self.assertIn("inbox unstated", out["detail"])

    def test_the_helper_reads_role_and_inbox_from_the_executed_vector(self):
        saved = hc._proc_argv_vector
        try:
            hc._proc_argv_vector = lambda pid: vector("standby")
            self.assertEqual(hc._watcher_role_and_inbox("bash x", 1), ("standby", INBOX))
            hc._proc_argv_vector = lambda pid: None
            self.assertEqual(hc._watcher_role_and_inbox("bash src/watch-tasks-stream.sh a b", 1),
                             (None, None))
        finally:
            hc._proc_argv_vector = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
