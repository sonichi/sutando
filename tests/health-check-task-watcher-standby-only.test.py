#!/usr/bin/env python3
"""The task-watcher probe must tell the supervisor's standby from the session watcher.

Both roles stamp the same per-instance sentinel (src/watch-tasks-stream.sh), so a
pool host whose worker ended its turn logged out ("Login expired · Please run
/login", its Monitor never re-armed) has a live sentinel naming the STANDBY the
notifier supervisor armed. Pre-fix the probe proved the pid alive and its script
the watcher, and reported "streaming watcher alive" for a seat that could do no
work (user feedback, 2026-09-29). Liveness was checked; capability was not.

The warning is a Claude-core reading. Only a Claude core runs the Monitor tool;
a Codex core's notifier always arms the standby (src/agent/codex/cli/start-cli.sh),
so there the standby IS the delivery path and a standby-only sentinel is ok. The
runtime is the WATCHER's: a worker's roster row records its own (spawn_worker
lets a Claude core host a Codex worker), and only a sentinel without a row falls
back to the core's config.
"""
import importlib.util
import json
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


def run(sentinels: dict, vectors: dict, unreadable=(), runtime="claude", roster=None) -> dict:
    """`sentinels` maps filename -> pid; `vectors` maps pid -> argv list, the
    OS-authoritative vector; a pid in `unreadable` has a flat argv only.
    `runtime` is what the host's config selects as the core runtime; an
    Exception instance is raised from the config read instead. `roster` is the
    text of state/roster.json (absent when None)."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "state" / "cores").mkdir(parents=True)
        if roster is not None:
            (ws / "state" / "roster.json").write_text(roster, encoding="utf-8")
        (ws / "state" / "cores" / "h.alive").write_text("{}")
        for fn, pid in sentinels.items():
            (ws / "state" / fn).write_text(str(pid))
        table = {str(p): v for p, v in vectors.items()}
        saved = (hc.WORKSPACE_DIR, hc._proc_argv, hc._proc_argv_vector, hc._watcher_trees,
                 hc._ps_snapshot, hc._pid_parent, hc._fresh_local_core_record,
                 hc._is_watcher_argv, hc.resolve_core_runtime)

        def selected(*_a, **_k):
            if isinstance(runtime, Exception):
                raise runtime
            return runtime
        try:
            hc.WORKSPACE_DIR = ws
            hc.resolve_core_runtime = selected
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
             hc._is_watcher_argv, hc.resolve_core_runtime) = saved


class OnAClaudeCore(unittest.TestCase):
    """The default `run` stubs: the host's config selects the Claude core runtime."""

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



class GroupedByInbox(unittest.TestCase):
    """A standby is a gap only for an inbox no live session-role watcher holds. Two
    sentinels can name watchers of one inbox (each is keyed by its process's own
    (agent, instance), and the standby and the session inherit different envs)."""

    def test_a_standby_beside_a_session_watcher_of_the_same_inbox_is_ok(self):
        out = run({f"watch-tasks-stream-{WID}.pid": 4242, "watch-tasks-stream-agent~a1b2.pid": 100},
                  {4242: vector("standby"), 100: vector("session")})
        self.assertEqual(out["status"], "ok", out)
        self.assertNotIn("STANDBY", out["detail"])

    def test_one_inbox_spelled_with_a_trailing_slash_or_through_a_symlink_is_one_inbox(self):
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "deliveries"
            real.mkdir()
            link = Path(td) / "via-link"
            link.symlink_to(real)
            for standby_at, session_at in ((str(link), str(real) + "/"),
                                           (str(real) + "//", str(link))):
                out = run({f"watch-tasks-stream-{WID}.pid": 4242, "watch-tasks-stream.pid": 100},
                          {4242: vector("standby", standby_at), 100: vector("session", session_at)})
                self.assertEqual(out["status"], "ok", (standby_at, session_at, out))

    def test_a_session_watcher_of_another_inbox_does_not_cover_the_standby(self):
        out = run({f"watch-tasks-stream-{WID}.pid": 4242, "watch-tasks-stream.pid": 100},
                  {4242: vector("standby"), 100: vector("session", INBOX + "-other")})
        self.assertEqual(out["status"], "warn", out)
        self.assertIn(f"watch-tasks-stream-{WID}.pid -> pid 4242", out["detail"])

    def test_a_second_standby_or_an_unread_vector_on_the_same_inbox_covers_nothing(self):
        for other, unreadable in ((vector("standby"), ()), (vector("session"), (100,))):
            out = run({f"watch-tasks-stream-{WID}.pid": 4242, "watch-tasks-stream.pid": 100},
                      {4242: vector("standby"), 100: other}, unreadable=unreadable)
            self.assertEqual(out["status"], "warn", (other, unreadable, out))
            self.assertIn("name only the STANDBY watcher", out["detail"])


class OnACodexCore(unittest.TestCase):
    """A Codex core cannot run Monitor: its notifier arms the standby on every start,
    so the standby is the normal delivery path and the warning must not fire."""

    def test_a_standby_only_sentinel_is_ok_and_says_the_standby_delivers(self):
        out = run({f"watch-tasks-stream-{WID}.pid": 4242}, {4242: vector("standby")},
                  runtime="codex")
        self.assertEqual(out["status"], "ok", out)
        self.assertIn("streaming watcher alive (pid 4242)", out["detail"])
        self.assertIn("standby watcher", out["detail"])
        self.assertIn("delivery path", out["detail"])
        self.assertIn(f"watch-tasks-stream-{WID}.pid -> pid 4242", out["detail"])
        self.assertNotIn("--role session --inbox", out["detail"])

    def test_a_core_beside_a_standby_only_worker_stays_ok_and_names_only_the_standby(self):
        out = run({"watch-tasks-stream.pid": 100, f"watch-tasks-stream-{WID}.pid": 4242},
                  {100: vector("session", "/ws/tasks"), 4242: vector("standby")},
                  runtime="codex")
        self.assertEqual(out["status"], "ok", out)
        self.assertIn("2 streaming watchers alive", out["detail"])
        self.assertIn("1 sentinel(s) name the standby watcher", out["detail"])
        self.assertNotIn("pid 100 (", out["detail"])

    def test_a_session_watcher_carries_no_standby_note(self):
        out = run({f"watch-tasks-stream-{WID}.pid": 4242}, {4242: vector("session")},
                  runtime="codex")
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["detail"], "streaming watcher alive (pid 4242)")

    def test_an_unreadable_config_proves_no_claude_core_so_the_warn_is_not_invented(self):
        out = run({f"watch-tasks-stream-{WID}.pid": 4242}, {4242: vector("standby")},
                  runtime=ValueError("sutando config: unsupported core.runtime"))
        self.assertEqual(out["status"], "ok", out)
        self.assertIn("standby watcher", out["detail"])


class TheWatchersOwnRuntime(unittest.TestCase):
    """The roster row outranks the core's config: a Claude core can host a Codex
    worker (spawn_worker's explicit runtime), and the reverse."""

    def test_a_codex_worker_on_a_claude_core_is_ok_and_names_the_standby(self):
        out = run({f"watch-tasks-stream-{WID}.pid": 4242}, {4242: vector("standby")},
                  runtime="claude",
                  roster=json.dumps({"workers": {WID: {"state": "live", "runtime": "codex"}}}))
        self.assertEqual(out["status"], "ok", out)
        self.assertIn("1 sentinel(s) name the standby watcher", out["detail"])
        self.assertIn(f"watch-tasks-stream-{WID}.pid -> pid 4242", out["detail"])

    def test_the_row_is_found_by_the_inbox_when_the_sentinel_name_is_bounded(self):
        out = run({"watch-tasks-stream-agent~a1b2c3.pid": 4242}, {4242: vector("standby")},
                  runtime="claude",
                  roster=json.dumps({"workers": {WID: {"state": "live", "runtime": "codex"}}}))
        self.assertEqual(out["status"], "ok", out)
        self.assertIn("standby watcher", out["detail"])

    def test_a_claude_worker_on_a_codex_core_still_warns(self):
        out = run({f"watch-tasks-stream-{WID}.pid": 4242}, {4242: vector("standby")},
                  runtime="codex",
                  roster=json.dumps({"workers": {WID: {"state": "live", "runtime": "claude"}}}))
        self.assertEqual(out["status"], "warn", out)
        self.assertIn("name only the STANDBY watcher", out["detail"])

    def test_a_row_without_a_runtime_falls_back_to_the_core(self):
        roster = json.dumps({"workers": {WID: {"state": "live"}}})
        warn = run({f"watch-tasks-stream-{WID}.pid": 4242}, {4242: vector("standby")},
                   runtime="claude", roster=roster)
        self.assertEqual(warn["status"], "warn", warn)
        ok = run({f"watch-tasks-stream-{WID}.pid": 4242}, {4242: vector("standby")},
                 runtime="codex", roster=roster)
        self.assertEqual(ok["status"], "ok", ok)

    def test_an_unreadable_or_foreign_roster_falls_back_to_the_core(self):
        for roster in ("{not json", "[]", json.dumps({"workers": {"other": {"runtime": "codex"}}}),
                       json.dumps({"workers": {WID: "codex"}})):
            out = run({f"watch-tasks-stream-{WID}.pid": 4242}, {4242: vector("standby")},
                      runtime="claude", roster=roster)
            self.assertEqual(out["status"], "warn", (roster, out))

    def test_a_claude_and_a_codex_standby_side_by_side_warn_and_note(self):
        other = "0" * 32
        out = run({f"watch-tasks-stream-{WID}.pid": 4242,
                   f"watch-tasks-stream-{other}.pid": 4343},
                  {4242: vector("standby"),
                   4343: vector("standby", "/ws/deliveries/" + other)},
                  runtime="claude",
                  roster=json.dumps({"workers": {other: {"state": "live", "runtime": "codex"}}}))
        self.assertEqual(out["status"], "warn", out)
        self.assertIn(f"name only the STANDBY watcher: watch-tasks-stream-{WID}.pid -> pid 4242 (",
                      out["detail"])
        self.assertIn("1 sentinel(s) name the standby watcher, the delivery path", out["detail"])
        self.assertIn(f"watch-tasks-stream-{other}.pid -> pid 4343", out["detail"])


class TheHelper(unittest.TestCase):
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
