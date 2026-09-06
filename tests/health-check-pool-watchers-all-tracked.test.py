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
import contextlib
import os
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


def run(sentinels: dict, trees: dict, argv=WATCHER_ARGV, core_alive=True,
        parent="1", pid_instance=None, pid_actor="") -> dict:
    """`sentinels` maps filename -> contents; `trees` maps root pid -> members.

    `pid_instance` is what the WATCHER's own environment yields: a string names
    its instance, "" is the default, and None means unreadable.
    """
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "state" / "cores").mkdir(parents=True)
        if core_alive:
            (ws / "state" / "cores" / "h.alive").write_text("{}")
        for fn, text in sentinels.items():
            (ws / "state" / fn).write_text(text)
        saved = (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees,
                 hc._ps_snapshot, hc._pid_parent, hc._fresh_local_core_record,
                 hc._pid_instance_id, hc._pid_actor_id)
        try:
            hc.WORKSPACE_DIR = ws
            hc._proc_argv = (argv if callable(argv) else (lambda pid: argv))
            hc._watcher_trees = lambda *a, **k: trees
            hc._ps_snapshot = lambda *a, **k: ""
            hc._pid_parent = lambda pid, ps=None: parent
            hc._fresh_local_core_record = lambda *a, **k: ({} if core_alive else None)
            hc._pid_instance_id = lambda pid: pid_instance
            hc._pid_actor_id = lambda pid: pid_actor
            return hc.check_task_watcher()
        finally:
            (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees,
             hc._ps_snapshot, hc._pid_parent, hc._fresh_local_core_record,
             hc._pid_instance_id, hc._pid_actor_id) = saved


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

    def test_a_dead_sentinel_beside_a_live_one_still_warns(self):
        """A clean exit REMOVES the sentinel (the cleanup trap), so a file that
        outlives its pid is a crash — and a live peer is a different instance,
        not evidence about this one.

        This assertion used to read `does_not_warn`, on the rationale that "a
        worker that exited leaves its file". That contradicts the probe's own
        docstring, and it pinned the false green rather than catching it.
        """
        argv = lambda pid: "" if str(pid) == "100" else WATCHER_ARGV  # noqa: E731
        r = run({"watch-tasks-stream.pid": "100\n",
                 "watch-tasks-stream-worker-1.pid": "200\n"},
                {"200": {"200"}}, argv=argv)
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("200", r["detail"])
        self.assertIn("100", r["detail"])
        self.assertIn("watch-tasks-stream.pid", r["detail"])

    def test_a_reused_pid_beside_a_live_one_still_warns(self):
        argv = lambda pid: ("/usr/bin/python3 unrelated" if str(pid) == "300"  # noqa: E731
                            else WATCHER_ARGV)
        r = run({"watch-tasks-stream.pid": "300\n",
                 "watch-tasks-stream-worker-1.pid": "200\n"},
                {"200": {"200"}}, argv=argv)
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("PID reuse", r["detail"])

    def test_an_unreadable_sentinel_beside_a_live_one_still_warns(self):
        r = run({"watch-tasks-stream.pid": "not-a-pid\n",
                 "watch-tasks-stream-worker-1.pid": "200\n"},
                {"200": {"200"}})
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("unreadable", r["detail"])

    def test_a_clean_pool_is_still_ok(self):
        """The negative control for the three above: nothing anomalous, no warn."""
        r = run({"watch-tasks-stream.pid": "200\n",
                 "watch-tasks-stream-worker-1.pid": "201\n"},
                {"200": {"200"}, "201": {"201"}})
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_the_repair_writes_the_path_the_check_resolved(self):
        """`fix_task_watcher_sentinel` used to re-derive the path from its own
        environment, so a repair could stamp a different instance's file than
        the one the check found missing."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "watch-tasks-stream-worker-9.pid"
            # WORKSPACE_DIR is pinned inside the tempdir so a regression that
            # re-derives the ambient path cannot reach a real workspace.
            saved = (hc._proc_argv, hc._is_watcher_argv, hc.WORKSPACE_DIR)
            try:
                hc._proc_argv = lambda pid: WATCHER_ARGV
                hc._is_watcher_argv = lambda argv: True
                hc.WORKSPACE_DIR = Path(td) / "ws"
                out = hc.fix_task_watcher_sentinel(
                    {"_sentinel_restamp_pid": "4242",
                     "_sentinel_restamp_path": str(target)})
            finally:
                (hc._proc_argv, hc._is_watcher_argv, hc.WORKSPACE_DIR) = saved
            self.assertTrue(target.exists(), out)
            self.assertFalse((Path(td) / "ws").exists(),
                             "the repair touched the ambient workspace")
            self.assertEqual(target.read_text().strip(), "4242")

    def test_the_repair_refuses_when_the_check_named_no_path(self):
        out = hc.fix_task_watcher_sentinel({"_sentinel_restamp_pid": "4242"})
        self.assertIn("no sentinel path", out)

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


class TheRestampTargetComesFromTheWATCHERsIdentity(unittest.TestCase):
    """A sentinel-less watcher is re-stamped at ITS path, never at this process's.

    The probe runs with the host's ambient environment. Deriving the repair target
    from that names the canonical file for a NAMED watcher: the wrong instance is
    claimed, and the watcher's own exit trap removes a different filename, leaving
    the bare sentinel stale and pointing at a pid that is not the canonical core's.
    """

    SUPERVISED = {"trees": {"901": {"901"}}, "parent": "900"}

    def test_a_named_watcher_is_re_stamped_at_its_own_path(self):
        r = run({}, **self.SUPERVISED, pid_instance="worker-7")
        self.assertEqual(r["status"], "warn")
        target = Path(r["_sentinel_restamp_path"]).name
        self.assertNotEqual(target, "watch-tasks-stream.pid",
                            "the canonical name claims the wrong instance")
        self.assertIn("worker-7", target)

    def test_the_default_watcher_still_gets_the_canonical_path(self):
        # The fix must not refuse the case it was already handling correctly.
        r = run({}, **self.SUPERVISED, pid_instance="")
        self.assertEqual(Path(r["_sentinel_restamp_path"]).name, "watch-tasks-stream.pid")

    def test_an_unreadable_identity_offers_NO_repair_target(self):
        r = run({}, **self.SUPERVISED, pid_instance=None)
        self.assertEqual(r["status"], "warn")
        self.assertNotIn("_sentinel_restamp_path", r,
                         "a guessed target is published as a repair instruction")
        self.assertIn("_sentinel_restamp_pid", r, "the pid is still reported")
        self.assertIn("unreadable", r["detail"])

    @contextlib.contextmanager
    def _health_check_identity(self, actor, instance):
        """Give HEALTH-CHECK a different identity than the watcher.

        Without this the two resolve alike and `agent=None` returns the right
        answer for the wrong reason, so the defect is invisible to every case above.
        """
        names = {"SUTANDO_AGENT_ID": actor, "SUTANDO_INSTANCE_ID": instance}
        saved = {k: os.environ.get(k) for k in names}
        os.environ.update(names)
        try:
            yield
        finally:
            for k, v in saved.items():
                os.environ[k] = v if v is not None else os.environ.pop(k, "")
                if v is None:
                    os.environ.pop(k, None)

    def test_the_target_follows_the_WATCHERs_identity_not_health_checks(self):
        with self._health_check_identity("health-b", "health-z"):
            r = run({}, **self.SUPERVISED, pid_instance="worker-7", pid_actor="watcher-a")
            self.assertEqual(Path(r["_sentinel_restamp_path"]).name,
                             "watch-tasks-stream-watcher-a+worker-7.pid",
                             "health-check's own actor leaked into the repair target")

    def test_an_observed_default_is_the_canonical_default_not_the_callers(self):
        # `inst or None` sent an OBSERVED default back to the caller's environment.
        with self._health_check_identity("health-b", "health-z"):
            r = run({}, **self.SUPERVISED, pid_instance="", pid_actor="watcher-a")
            self.assertEqual(Path(r["_sentinel_restamp_path"]).name,
                             "watch-tasks-stream-watcher-a.pid")

    def test_an_unreadable_ACTOR_also_offers_no_target(self):
        # The instance half already refused; the actor half must refuse alike.
        r = run({}, **self.SUPERVISED, pid_instance="worker-7", pid_actor=None)
        self.assertNotIn("_sentinel_restamp_path", r)

    def test_the_watcher_is_never_prescribed_for_stopping_in_any_of_them(self):
        for inst in ("worker-7", "", None):
            with self.subTest(instance=inst):
                r = run({}, **self.SUPERVISED, pid_instance=inst)
                self.assertIn("Do NOT stop it", r["detail"])


class ThePsFallbackProvesAbsenceNeverAValue(unittest.TestCase):
    """`ps` concatenates argv and env with spaces, so a value containing one is
    unrecoverable: `worker 7` reads back as `worker`.

    That is not a fails-safe error — it names a DIFFERENT sentinel, and the pair
    are genuinely distinct keys (`watcher-a+worker%207` vs `watcher-a+worker`),
    so the repair would re-stamp another instance's file. The fallback can prove
    the environment was printed and the name absent; it cannot prove a value.
    """

    class _R:
        def __init__(self, out):
            self.stdout, self.returncode, self.stderr = out, 0, ""

    def _read(self, out):
        saved = hc.subprocess.run
        try:
            hc.subprocess.run = lambda *a, **k: self._R(out)
            return hc._pid_env_first("999", ("SUTANDO_INSTANCE_ID",))
        finally:
            hc.subprocess.run = saved

    def test_a_whitespace_value_is_refused_not_truncated(self):
        self.assertIsNone(self._read("sleep 8 FOO=1 SUTANDO_INSTANCE_ID=worker 7 BAR=2"))

    def test_even_a_plain_value_is_refused_because_wholeness_is_unprovable(self):
        self.assertIsNone(self._read("sleep 8 FOO=1 SUTANDO_INSTANCE_ID=worker-7 BAR=2"))

    def test_the_control_a_printed_environment_can_still_prove_absence(self):
        # Or the refusal could pass by answering None to everything.
        self.assertEqual(self._read("sleep 8 FOO=1 BAR=2"), "")

    def test_the_control_no_environment_at_all_is_still_unreadable(self):
        self.assertIsNone(self._read("sleep 8"))

    def test_the_two_identities_it_would_have_confused_are_distinct(self):
        # The premise: if these collapsed, truncation would be harmless.
        sys.path.insert(0, str(ROOT / "src" / "runtime-api"))
        ik = importlib.import_module("instance_key")
        self.assertNotEqual(ik.instance_key("watcher-a", "worker 7"),
                            ik.instance_key("watcher-a", "worker"))


class TheActorPrecedenceComesFromItsOwner(unittest.TestCase):
    """A consumer reading another process's identity must use the same order the
    owner uses; a second copy of the list is how the two answer differently."""

    def test_health_check_reads_the_owners_list(self):
        sys.path.insert(0, str(ROOT / "src" / "runtime-api"))
        rundir = importlib.import_module("rundir")
        self.assertEqual(tuple(hc.actor_env_names()), tuple(rundir.ACTOR_ENV_NAMES))

    def test_no_local_copy_of_the_list_remains(self):
        src = (ROOT / "src" / "health-check.py").read_text()
        self.assertNotIn('"AGENT_MXID"', src,
                         "the precedence is spelled here again, so it can drift")


class TheWatcherPredicateIsAShapeNotAFieldCount(unittest.TestCase):
    """`len(parts) != 2` rejected two shapes that actually run.

    The Codex notifier execs the script WITH a tasks directory, and an install
    path containing a space splits into more tokens again -- so a production
    watcher read as "not a watcher", which makes a duplicate invisible to the
    tree count and hides the running process from the re-stamp branch. Every
    existing case used the synthetic two-token form, so none of them could see it.
    """

    REAL = [
        ("the synthetic form the older cases use", "bash src/watch-tasks-stream.sh"),
        ("the notifier's production exec", "bash /repo/src/watch-tasks-stream.sh /w/tasks"),
        ("an app checkout whose path has a space",
         "bash /Users/x/Library/Application Support/Sutando/src/watch-tasks-stream.sh"),
        ("both at once", "bash /Users/x/Application Support/src/watch-tasks-stream.sh /w/my tasks"),
    ]
    IMPOSTORS = [
        ("a grep FOR the script", "grep watch-tasks-stream"),
        ("a shell -c that merely mentions it", "bash -c ps | grep watch-tasks-stream"),
        ("a similarly named script", "bash /repo/src/x-watch-tasks-stream.sh"),
        ("the name inside a longer filename", "bash /repo/src/watch-tasks-stream.sh.bak"),
        ("an unrelated shell script", "bash /repo/src/startup.sh"),
    ]

    def test_every_real_launch_shape_is_recognised(self):
        for label, argv in self.REAL:
            with self.subTest(shape=label):
                self.assertTrue(hc._is_watcher_argv(argv), argv)

    def test_the_controls_impostors_are_still_refused(self):
        # Without these the predicate could pass by accepting anything.
        for label, argv in self.IMPOSTORS:
            with self.subTest(shape=label):
                self.assertFalse(hc._is_watcher_argv(argv), argv)

    def test_a_production_shaped_duplicate_is_counted_as_a_second_tree(self):
        # The consequence: two real watchers read as one, so the duplicate the
        # probe exists to find is invisible.
        ps = ("  100 1 bash /repo/src/watch-tasks-stream.sh /w/tasks\n"
              "  200 1 bash /repo/src/watch-tasks-stream.sh /w/tasks\n")
        self.assertEqual(len(hc._watcher_trees(ps)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
