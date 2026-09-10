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
from unittest.mock import patch

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
        parent="1", pid_instance=None, pid_actor="", targets=None) -> dict:
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
                 hc._pid_instance_id, hc._pid_actor_id, hc._watcher_sentinel_target)
        try:
            hc.WORKSPACE_DIR = ws
            hc._proc_argv = (argv if callable(argv) else (lambda pid: argv))
            hc._watcher_trees = lambda *a, **k: trees
            hc._ps_snapshot = lambda *a, **k: ""
            hc._pid_parent = lambda pid, ps=None: parent
            hc._fresh_local_core_record = lambda *a, **k: ({} if core_alive else None)
            hc._pid_instance_id = lambda pid: pid_instance
            hc._pid_actor_id = lambda pid: pid_actor
            if targets is not None:
                # `{}` states that no pid resolves; None keeps the production
                # resolver, which reads THIS host — only the restamp cases want that.
                hc._watcher_sentinel_target = (
                    lambda sd, pid, _m=targets: (
                        (ws / "state" / _m[str(pid)]) if str(pid) in _m else None))
            return hc.check_task_watcher()
        finally:
            (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees,
             hc._ps_snapshot, hc._pid_parent, hc._fresh_local_core_record,
             hc._pid_instance_id, hc._pid_actor_id,
             hc._watcher_sentinel_target) = saved


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
        """888 resolves to the SAME sentinel target as tracked 200, so it really
        is a duplicate. Without the identity this case pinned the unsafe premise
        that a second root is a duplicate by arithmetic alone."""
        r = run({"watch-tasks-stream-worker-1.pid": "200\n"},
                {"200": {"200"}, "888": {"888"}},
                targets={"200": "watch-tasks-stream-worker-1.pid",
                         "888": "watch-tasks-stream-worker-1.pid"})
        self.assertEqual(r["status"], "warn")
        self.assertIn("888", r["detail"])
        self.assertIn("Keep the tracked one(s) (200)", r["detail"])

    def test_a_stranger_carrying_the_script_NAME_is_not_a_live_watcher(self):
        """The sentinel's pid must be validated by the module's own predicate.

        A substring test on argv accepts any process that merely mentions
        `watch-tasks-stream.sh` -- e.g. as a data argument -- and reports the
        watcher healthy while nothing drains tasks/.
        """
        impostor = "/usr/bin/python3 -c import time;time.sleep(9) watch-tasks-stream.sh"
        r = run({"watch-tasks-stream-worker-1.pid": "200\n"}, {}, argv=impostor)
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertNotIn("watcher is running", r["detail"])
        self.assertTrue(
            "not the watcher" in r["detail"] or "UNKNOWN" in r["detail"],
            f"a stranger must read as PID reuse or UNKNOWN, got: {r['detail']}")

    def test_a_DISTINCT_target_is_a_separate_instance_not_a_duplicate(self):
        """worker-b is not worker-a running twice. Telling an operator to reduce
        the count here removes the only watcher for that instance."""
        r = run({"watch-tasks-stream-worker-a.pid": "901\n"},
                {"901": {"901"}, "902": {"902"}},
                targets={"901": "watch-tasks-stream-worker-a.pid",
                         "902": "watch-tasks-stream-worker-b.pid"})
        self.assertEqual(r["status"], "warn")
        self.assertIn("DIFFERENT instance", r["detail"])
        self.assertIn("Do NOT stop", r["detail"])
        self.assertNotIn("duplicate its work", r["detail"])

    def test_an_UNREADABLE_identity_authorises_no_action(self):
        """Unknown identity must not license stop OR reduce: the same sentence
        recreates a duplicate for one instance and orphans another."""
        r = run({"watch-tasks-stream-worker-a.pid": "901\n"},
                {"901": {"901"}, "903": {"903"}},
                targets={"901": "watch-tasks-stream-worker-a.pid"})
        self.assertEqual(r["status"], "warn")
        self.assertIn("UNKNOWN", r["detail"])
        self.assertIn("903", r["detail"])
        self.assertNotIn("safe to stop", r["detail"])
        self.assertNotIn("reduce the count", r["detail"])

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
                hc._is_watcher_argv = lambda argv, pid=None: True
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

    def test_all_sentinels_dead_with_watchers_running_names_and_classifies(self):
        # "orphaned" applied to every root told an operator to stop a supervised
        # watcher; the verdict must say which group each root is in.
        r = run({"watch-tasks-stream.pid": "100\n"}, {"777": {"777"}}, argv="")
        self.assertEqual(r["status"], "warn")
        self.assertIn("777", r["detail"])
        self.assertIn("ownerless", r["detail"])
        self.assertIn("supervised", r["detail"])

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


class TheIdentityProbesAreTriStateNotBoolean(unittest.TestCase):
    """`None` means CANNOT READ; "" means the process states none. A probe that
    collapses them publishes THIS process's identity as the watcher's."""

    def test_environ_read_returns_the_first_name_that_is_set(self):
        blob = b"HOME=/root\0SUTANDO_INSTANCE=inst-b\0SUTANDO_AGENT=agent-z\0"
        with patch.object(Path, "read_bytes", return_value=blob):
            self.assertEqual(
                hc._pid_env_first("4242", ["SUTANDO_INSTANCE", "SUTANDO_AGENT"]), "inst-b")

    def test_a_name_set_but_empty_falls_through_to_the_next(self):
        blob = b"SUTANDO_INSTANCE=\0SUTANDO_AGENT=agent-z\0"
        with patch.object(Path, "read_bytes", return_value=blob):
            self.assertEqual(
                hc._pid_env_first("4242", ["SUTANDO_INSTANCE", "SUTANDO_AGENT"]), "agent-z")

    def test_none_of_the_names_present_states_none_rather_than_cannot_read(self):
        with patch.object(Path, "read_bytes", return_value=b"HOME=/root\0"):
            self.assertEqual(hc._pid_env_first("4242", ["SUTANDO_INSTANCE"]), "")

    def test_an_unreadable_process_is_None_not_empty(self):
        with patch.object(Path, "read_bytes", side_effect=OSError("no /proc")), \
             patch.object(hc.subprocess, "run", side_effect=OSError("no ps")):
            self.assertIsNone(hc._pid_env_first("4242", ["SUTANDO_INSTANCE"]))

    def test_no_stated_default_yields_no_repair_target(self):
        # Naming a target from a guessed identity is what gets a stranger killed.
        with patch.object(hc, "_pid_instance_id", return_value="i"), \
             patch.object(hc, "_pid_actor_id", return_value="a"), \
             patch.object(hc, "stated_default_identity", return_value=None):
            self.assertIsNone(hc._watcher_sentinel_target(Path("/tmp"), "4242"))

    def test_a_raising_resolver_yields_no_repair_target(self):
        with patch.object(hc, "_pid_instance_id", return_value="i"), \
             patch.object(hc, "_pid_actor_id", return_value="a"), \
             patch.object(hc, "stated_default_identity", side_effect=RuntimeError("x")):
            self.assertIsNone(hc._watcher_sentinel_target(Path("/tmp"), "4242"))

    def test_a_short_ps_line_is_skipped_not_parsed(self):
        # A header row or a truncated line has no argv column to judge.
        parent, live = hc._ps_watcher_index("  PID PPID\n 100 1 bash src/watch-tasks-stream.sh\n")
        self.assertIn("100", parent)
        self.assertNotIn("PID", parent)


class TheDarwinArgvParseIsExercisedOnAnyPlatform(unittest.TestCase):
    """KERN_PROCARGS2's layout is parsed by hand, so the loops need a test.

    On a linux runner /proc answers first and this branch never executes, so the
    NUL-skipping and the argc bound would ship unmeasured.
    """

    @staticmethod
    def _fake_libc(payload):
        import ctypes

        class _Libc:
            def sysctl(self, mib, n, buf, sizep, _a, _b):
                ctypes.memmove(buf, payload, len(payload))
                sizep._obj.value = len(payload)
                return 0

        return _Libc()

    def _vector(self, argc, exec_path, argv, pad=b"", lead=b""):
        import ctypes
        import ctypes.util  # noqa: F401 -- must load BEFORE CDLL is patched
        blob = (argc.to_bytes(4, sys.byteorder) + lead + exec_path + b"\0" + pad
                + b"\0".join(argv) + b"\0")
        with patch.object(Path, "read_bytes", side_effect=OSError("not linux")), \
             patch.object(ctypes, "CDLL", return_value=self._fake_libc(blob)):
            return hc._proc_argv_vector(4242)

    def test_the_exec_path_is_skipped_and_argv_returned(self):
        self.assertEqual(
            self._vector(2, b"/bin/bash", [b"bash", b"/repo/src/watch-tasks-stream.sh"]),
            ["bash", "/repo/src/watch-tasks-stream.sh"])

    def test_padding_nuls_between_exec_path_and_argv_are_skipped(self):
        self.assertEqual(
            self._vector(2, b"/bin/bash", [b"bash", b"/w/x.sh"], pad=b"\0\0\0"),
            ["bash", "/w/x.sh"])

    def test_leading_nuls_before_the_exec_path_are_skipped(self):
        # The first skip loop only runs when the blob is padded ahead of the
        # exec path; without a case for it the branch ships unmeasured.
        self.assertEqual(
            self._vector(2, b"/bin/bash", [b"bash", b"/w/y.sh"], lead=b"\0\0"),
            ["bash", "/w/y.sh"])

    def test_argc_bounds_the_result_so_envp_is_not_read_as_argv(self):
        # argc=1, but the environment follows argv in the same blob.
        self.assertEqual(
            self._vector(1, b"/bin/bash", [b"bash", b"PATH=/usr/bin", b"HOME=/root"]),
            ["bash"])

    def test_a_failing_sysctl_is_None_not_a_partial_vector(self):
        import ctypes
        import ctypes.util  # noqa: F401 -- must load BEFORE CDLL is patched

        class _Fail:
            def sysctl(self, *a):
                return -1

        with patch.object(Path, "read_bytes", side_effect=OSError("not linux")), \
             patch.object(ctypes, "CDLL", return_value=_Fail()):
            self.assertIsNone(hc._proc_argv_vector(4242))


class TheWatcherPredicateIsAShapeNotAFieldCount(unittest.TestCase):
    """`len(parts) != 2` rejected two shapes that actually run.

    The Codex notifier execs the script WITH a tasks directory, and an install
    path containing a space splits into more tokens again -- so a production
    watcher read as "not a watcher", which makes a duplicate invisible to the
    tree count and hides the running process from the re-stamp branch. Every
    existing case used the synthetic two-token form, so none of them could see it.
    """

    # The script token carries the name, so every way of re-splitting the flattened
    # argv still yields a watcher -- these are decidable from the string alone.
    REAL = [
        ("the synthetic form the older cases use", "bash src/watch-tasks-stream.sh"),
        ("the notifier's production exec", "bash /repo/src/watch-tasks-stream.sh /w/tasks"),
    ]
    # A space in the INSTALL path pushes the name out of the script token, and then
    # "one spaced path" and "a script plus arguments" are the same string.
    REAL_UNDECIDABLE = [
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
                # Reviewer-directed, #3875 at ad7f1bf7: without the OS vector a
                # flattened argv is UNKNOWN, so it may not authorize repair.
                self.assertIsNot(hc._is_watcher_argv(argv), False, argv)

    def test_an_undecidable_real_shape_is_never_REJECTED(self):
        # The defect this class was written for: a production watcher read as "not
        # a watcher". UNKNOWN keeps it counted; only False would resurrect that.
        for label, argv in self.REAL_UNDECIDABLE:
            with self.subTest(shape=label):
                self.assertIsNot(hc._is_watcher_argv(argv), False, argv)

    def test_an_undecidable_real_shape_still_counts_as_a_tree(self):
        # What the caller does with UNKNOWN is the behaviour the probe depends on.
        for label, argv in self.REAL_UNDECIDABLE:
            with self.subTest(shape=label):
                self.assertTrue(hc._watcher_trees("  100 1 %s\n" % argv), argv)

    def test_the_controls_impostors_are_still_refused(self):
        # assertIs, not assertFalse: None must not be able to satisfy the control.
        for label, argv in self.IMPOSTORS:
            with self.subTest(shape=label):
                self.assertIs(hc._is_watcher_argv(argv), False, argv)

    def test_a_production_shaped_duplicate_is_counted_as_a_second_tree(self):
        # The consequence: two real watchers read as one, so the duplicate the
        # probe exists to find is invisible.
        ps = ("  100 1 bash /repo/src/watch-tasks-stream.sh /w/tasks\n"
              "  200 1 bash /repo/src/watch-tasks-stream.sh /w/tasks\n")
        self.assertEqual(len(hc._watcher_trees(ps)), 2)


class WhitespaceInsideAPathnameIsNotAnArgvBoundary(unittest.TestCase):
    """Reported by qingyun-wu on #3875 at 631e7fea, with real launched processes.

    The vector branch matched the script NAME anywhere in vec[1] via a regex whose
    `[\\s/]` alternative treats a space as a component boundary. Inside one
    authoritative pathname it is not: `backup/copy watch-tasks-stream.sh` is a file
    named `copy watch-tasks-stream.sh`, and publishing its pid points cleanup at an
    unrelated process. Compare the exact final component instead.
    """

    TABLE = [("plain/src/watch-tasks-stream.sh", True),
             ("spaced dir/src/watch-tasks-stream.sh", True),
             ("backup/watch-tasks-stream.sh backup", False),
             ("backup/copy watch-tasks-stream.sh", False),
             ("backup/watch-tasks-stream.sh.bak", False)]

    def _with_vector(self, path):
        orig = hc._proc_argv_vector
        hc._proc_argv_vector = lambda pid: ["/bin/bash", path]
        try:
            return hc._is_watcher_argv("bash " + path, 4242)
        finally:
            hc._proc_argv_vector = orig

    def test_the_reported_table_holds(self):
        for path, want in self.TABLE:
            self.assertIs(self._with_vector(path), want, path)

    def test_a_name_that_is_not_the_final_component_is_refused(self):
        # The two rows that published a wrong pid; each fails if the regex returns.
        for path in ("backup/watch-tasks-stream.sh backup", "backup/copy watch-tasks-stream.sh"):
            self.assertIs(self._with_vector(path), False, path)

    def test_a_spaced_DIRECTORY_still_recognises_a_real_watcher(self):
        # The control that stops the fix from becoming "reject anything with a space".
        self.assertIs(self._with_vector("spaced dir/src/watch-tasks-stream.sh"), True)



class AnUnreadableVectorSaysUnknownNeverTrue(unittest.TestCase):
    """Reviewer-directed, #3875 at ad7f1bf7.

    `assertIsNot(..., False)` in the launch-shape test is satisfied by True, so
    relaxing it left the fallback unpinned. These assert the positive shape: an
    ambiguous flattened argv is UNKNOWN, an unambiguous one is still True, and
    UNKNOWN is counted as a tree rather than dropped.
    """

    AMBIGUOUS = "bash /repo/watch-tasks-stream.sh backup"
    EXACT = "bash /repo/src/watch-tasks-stream.sh"

    def _flat(self, argv):
        orig = hc._proc_argv_vector
        hc._proc_argv_vector = lambda pid: None
        try:
            return hc._is_watcher_argv(argv, 4242)
        finally:
            hc._proc_argv_vector = orig

    def test_ambiguous_flattened_argv_is_unknown_not_true(self):
        self.assertIsNone(self._flat(self.AMBIGUOUS),
            "a flattened argv with trailing tokens cannot authorize repair")

    def test_argv_ending_at_the_script_is_still_true(self):
        self.assertIs(self._flat(self.EXACT), True,
            "the unambiguous two-token shape must stay recognised")

    def test_unknown_is_still_counted_as_a_tree(self):
        trees = hc._watcher_trees("  4242 1 %s\n" % self.AMBIGUOUS)
        self.assertTrue(trees, "UNKNOWN must count as a watcher, never read as absent")



class StopAdviceNeverTargetsASupervisedWatcher(unittest.TestCase):
    """Reported twice by qingyun-wu on #3875 (ad7f1bf7, f91511cc).

    The multiple-root warning called every root orphaned and said "stop them and
    restart one cleanly", then APPENDED the ownership split without retracting
    that instruction. An operator following it takes a healthy peer offline.
    """

    W = "/repo/src/watch-tasks-stream.sh"

    def _detail(self, ps, targets=None):
        """`targets` maps pid -> sentinel target. Absent, identity is unreadable,
        which is itself a case: the duplicate claim must not be made from a count."""
        orig = (hc._ps_snapshot, hc._proc_argv_vector, hc.watcher_sentinel_paths,
                hc._watcher_sentinel_target)
        hc._ps_snapshot = lambda: ps
        hc._proc_argv_vector = lambda pid: None
        hc.watcher_sentinel_paths = lambda sd: []
        if targets is not None:
            hc._watcher_sentinel_target = lambda sd, pid, _m=targets: _m.get(str(pid))
        try:
            return hc.check_task_watcher().get("detail") or ""
        finally:
            (hc._ps_snapshot, hc._proc_argv_vector, hc.watcher_sentinel_paths,
             hc._watcher_sentinel_target) = orig

    def test_two_supervised_roots_are_never_told_to_stop(self):
        d = self._detail(f"  900 1 /bin/zsh -l\n  901 900 bash {self.W}\n  902 900 bash {self.W}\n")
        self.assertNotIn("stop them", d, "blanket stop advice over supervised roots")
        self.assertIn("Do NOT stop any of them", d)
        self.assertIn("supervised: 901, 902", d)

    def test_mixed_ownership_names_only_the_ownerless_as_stoppable(self):
        # 901 is supervised (ppid 900 alive), 903 is ownerless (ppid 1). Reported by
        # qingyun-wu on #3875: the blanket form told an operator to stop 901 too.
        d = self._detail(f"  900 1 /bin/zsh -l\n  901 900 bash {self.W}\n  903 1 bash {self.W}\n")
        self.assertIn("Stop ONLY the ownerless (903)", d)
        self.assertIn("Do NOT stop 901", d)
        self.assertNotIn("stop them and restart one cleanly", d,
            "the blanket instruction must not survive when a supervised root is present")
        self.assertIn("ownerless: 903", d)
        self.assertIn("supervised: 901", d)

    def test_an_all_ownerless_set_is_still_stoppable(self):
        # The control: the fix must not become "never stop anything".
        d = self._detail(f"  900 1 /bin/zsh -l\n  903 1 bash {self.W}\n  904 1 bash {self.W}\n")
        self.assertIn("2 orphaned watcher(s)", d)
        self.assertIn("stop them and restart one cleanly", d)
        self.assertNotIn("Do NOT stop", d)

    MIXED = None  # set in the cases below

    def _mixed(self, targets):
        """Two SUPERVISED roots (901,902 under 900) plus one ownerless (903)."""
        return self._detail(
            f"  900 1 /bin/zsh -l\n  901 900 bash {self.W}\n  902 900 bash {self.W}\n"
            f"  903 1 bash {self.W}\n", targets=targets)

    def test_mixed_ownership_DISTINCT_supervised_are_not_reduced(self):
        """qingyun-wu, #3875: the mixed branch selected its advice before the
        identity guard, so it said 'do not reduce' and 'reduce those' at once."""
        d = self._mixed({"901": "/s/a.pid", "902": "/s/b.pid", "903": "/s/c.pid"})
        self.assertIn("DISTINCT", d)
        self.assertIn("Stop ONLY the ownerless (903)", d)
        self.assertNotIn("reduce those through the launcher", d,
            "distinct supervised instances must not be offered for reduction")

    def test_mixed_ownership_UNKNOWN_identity_is_not_reduced(self):
        d = self._mixed({})   # stated: no pid resolves
        self.assertIn("UNKNOWN", d)
        self.assertIn("Stop ONLY the ownerless (903)", d)
        self.assertNotIn("reduce those through the launcher", d,
            "unreadable identity must not authorise reduction in the mixed branch either")

    def test_reduction_reads_the_SUPERVISED_subset_not_every_root(self):
        """901 and 902 are distinct instances; the OWNERLESS 903 duplicates 901.

        Grouping over all roots finds a duplicate pair and would offer the
        supervised pair for reduction — but the duplicate is not among them.
        Reduction is advice about the supervised subset, so only that subset's
        identity may license it.
        """
        d = self._mixed({"901": "/s/a.pid", "902": "/s/b.pid", "903": "/s/a.pid"})
        self.assertIn("Stop ONLY the ownerless (903)", d)
        self.assertNotIn("reduce those through the launcher", d,
            "the duplicate is 903 (ownerless); 901 and 902 are distinct and must not be reduced")

    def test_mixed_ownership_a_PROVEN_duplicate_is_still_reduced(self):
        """The advice must survive where it is true, or the gate is just a mute."""
        d = self._mixed({"901": "/s/a.pid", "902": "/s/a.pid", "903": "/s/c.pid"})
        self.assertIn("processed 2x", d)
        self.assertIn("reduce those through the launcher", d)

    def test_the_duplicate_processing_cost_is_still_stated(self):
        """A PROVEN duplicate — both roots resolve to one target — must still say
        what it costs. The identity is what makes the claim true."""
        d = self._detail(f"  900 1 /bin/zsh -l\n  901 900 bash {self.W}\n  902 900 bash {self.W}\n",
                         targets={"901": "/s/w-a.pid", "902": "/s/w-a.pid"})
        self.assertIn("processed 2x", d,
            "the reason a duplicate matters must survive the softened advice")

    def test_two_DISTINCT_instances_are_not_called_duplicates(self):
        """qingyun-wu, #3875: losing the last sentinel must not turn two instances
        into duplicate workers, and must not authorise reducing the count."""
        d = self._detail(f"  900 1 /bin/zsh -l\n  901 900 bash {self.W}\n  902 900 bash {self.W}\n",
                         targets={"901": "/s/w-a.pid", "902": "/s/w-b.pid"})
        self.assertIn("DISTINCT", d)
        self.assertNotIn("processed 2x", d)
        self.assertIn("do not reduce the count", d)
        self.assertNotIn("reduce the count through the launcher", d,
            "the affirmative reduce instruction must not survive a distinct-instance verdict")

    def test_unreadable_identity_does_not_authorise_reduction(self):
        """No identity, no duplicate claim: it may be one instance twice or two once."""
        d = self._detail(f"  900 1 /bin/zsh -l\n  901 900 bash {self.W}\n  902 900 bash {self.W}\n",
                         targets={})   # stated: no pid resolves
        self.assertIn("UNKNOWN", d)
        self.assertNotIn("processed 2x", d)
        self.assertIn("do not reduce the count", d)
        self.assertNotIn("reduce the count through the launcher", d,
            "unreadable identity must not authorise reduction")



if __name__ == "__main__":
    unittest.main(verbosity=2)
