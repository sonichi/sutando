#!/usr/bin/env python3
"""`src/watcher_identity.py`: anchored, tri-state watcher identity.

Two properties, each with the failure it forbids:

  * anchored -- a process whose argv merely MENTIONS `watch-tasks-stream.sh` as
    an operand of another program (an observer, a `ps | grep`) is NOT a watcher;
  * tri-state -- a `ps` that failed, timed out or answered non-zero is
    unobserved, never "absent": the caller gets None, not False.

The health check keeps its private names as wrappers over this module, so the
delegation is pinned here too: no second copy of the anchor may exist.

Run: python3 tests/watcher-identity.test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import watcher_identity as wid  # noqa: E402

_spec = importlib.util.spec_from_file_location("hc", ROOT / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
sys.modules["hc"] = hc
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass

INBOX = "/ws/deliveries/" + "d" * 32
OBSERVER = f"python3 observer.py src/watch-tasks-stream.sh {INBOX}"
GENUINE = f"bash src/watch-tasks-stream.sh {INBOX}"


def vector(*argv):
    """An argv reader that answers this list for every pid."""
    return lambda pid: list(argv)


def unreadable(pid):
    return None


class TestAnchoredMatch(unittest.TestCase):
    def test_a_mention_as_an_operand_of_another_program_is_not_a_watcher(self):
        # Flattened only, and with the authoritative vector: both must say False.
        self.assertIs(wid.is_watcher_argv(OBSERVER, 4242, argv_vector=unreadable), False)
        self.assertIs(wid.is_watcher_argv(
            OBSERVER, 4242,
            argv_vector=vector("python3", "observer.py", "src/watch-tasks-stream.sh", INBOX)),
            False)

    def test_a_shell_grepping_for_the_script_is_not_a_watcher(self):
        argv = "bash -c ps -Ao args | grep watch-tasks-stream.sh"
        self.assertIs(wid.is_watcher_argv(argv, 4242, argv_vector=unreadable), False)
        self.assertIs(wid.is_watcher_argv(
            argv, 4242, argv_vector=vector("bash", "-c", "ps -Ao args | grep watch-tasks-stream.sh")),
            False)

    def test_a_longer_name_ending_in_the_script_is_not_a_watcher(self):
        self.assertIs(wid.is_watcher_argv("bash x-watch-tasks-stream.sh", 4242,
                                          argv_vector=unreadable), False)
        self.assertIs(wid.is_watcher_argv("bash src/x-watch-tasks-stream.sh", 4242,
                                          argv_vector=vector("bash", "src/x-watch-tasks-stream.sh")),
                      False)

    def test_the_executed_script_is_a_watcher_and_its_operands_are_returned(self):
        v = wid.classify_argv(GENUINE, 4242,
                              argv_vector=vector("/bin/bash", "/repo/src/watch-tasks-stream.sh", INBOX))
        self.assertIs(v.watcher, True)
        self.assertEqual(v.operands, [INBOX])

    def test_a_spaced_inbox_survives_when_the_vector_is_authoritative(self):
        spaced = "/ws/Application Support/deliveries/" + "d" * 32
        v = wid.classify_argv(f"bash src/watch-tasks-stream.sh {spaced}", 4242,
                              argv_vector=vector("bash", "src/watch-tasks-stream.sh", spaced))
        self.assertEqual((v.watcher, v.operands), (True, [spaced]))

    def test_a_bare_invocation_is_decidable_from_the_flattened_argv(self):
        v = wid.classify_argv("bash src/watch-tasks-stream.sh", 4242, argv_vector=unreadable)
        self.assertEqual((v.watcher, v.operands), (True, []))

    def test_a_flattened_argv_with_operands_is_undecidable_without_the_vector(self):
        """A spaced script path and a script plus operands are the same string."""
        v = wid.classify_argv(GENUINE, 4242, argv_vector=unreadable)
        self.assertEqual((v.watcher, v.operands), (None, None))
        self.assertIsNone(wid.is_watcher_argv(GENUINE))          # no pid: no vector

    def test_the_vector_outranks_a_misleading_flattened_argv(self):
        """The flat column says watcher; the executed program is not a shell."""
        self.assertIs(wid.is_watcher_argv("bash src/watch-tasks-stream.sh", 4242,
                                          argv_vector=vector("python3", "src/watch-tasks-stream.sh")),
                      False)


class TestTriStateInspection(unittest.TestCase):
    def _ps(self, rc, stdout="", raises=None):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(argv, rc, stdout, "")
        run.calls = calls
        return run

    def test_a_timed_out_ps_is_unobserved_not_absent(self):
        run = self._ps(0, raises=subprocess.TimeoutExpired(["ps"], 10))
        got = wid.inspect_pid(4242, run=run, argv_vector=unreadable)
        self.assertFalse(got.observed)
        self.assertIsNone(got.watcher)
        self.assertIn("timed out", got.reason)

    def test_a_non_zero_ps_is_unobserved_not_absent(self):
        got = wid.inspect_pid(4242, run=self._ps(1), argv_vector=unreadable)
        self.assertEqual((got.observed, got.watcher, got.operands), (False, None, None))

    def test_a_ps_that_cannot_run_is_unobserved(self):
        got = wid.inspect_pid(4242, run=self._ps(0, raises=FileNotFoundError("ps")),
                              argv_vector=unreadable)
        self.assertEqual((got.observed, got.watcher), (False, None))

    def test_an_empty_answer_is_unobserved(self):
        got = wid.inspect_pid(4242, run=self._ps(0, "\n"), argv_vector=unreadable)
        self.assertEqual((got.observed, got.watcher), (False, None))

    def test_an_observed_non_watcher_is_false(self):
        got = wid.inspect_pid(4242, run=self._ps(0, OBSERVER + "\n"), argv_vector=unreadable)
        self.assertEqual((got.observed, got.watcher, got.operands), (True, False, None))
        self.assertEqual(got.argv, OBSERVER)

    def test_an_observed_watcher_carries_its_operands(self):
        got = wid.inspect_pid(4242, run=self._ps(0, GENUINE + "\n"),
                              argv_vector=vector("bash", "src/watch-tasks-stream.sh", INBOX))
        self.assertEqual((got.observed, got.watcher, got.operands), (True, True, [INBOX]))

    def test_an_observed_but_undecidable_argv_stays_none(self):
        got = wid.inspect_pid(4242, run=self._ps(0, GENUINE + "\n"), argv_vector=unreadable)
        self.assertEqual((got.observed, got.watcher), (True, None))
        self.assertTrue(got.reason)

    def test_the_probe_reads_the_command_column_only(self):
        """Never `ps e`: the environment column carries credentials."""
        run = self._ps(1)
        wid.inspect_pid(4242, run=run)
        argv = run.calls[0]
        self.assertEqual(argv[:3], ["ps", "-o", "command="])
        self.assertNotIn("e", [a.lstrip("-") for a in argv if a.startswith("-")])


class TestAgainstRealProcesses(unittest.TestCase):
    """Positive control for the instrument: a real ps and a real argv read."""

    def _spawn(self, argv):
        p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        for _ in range(50):
            if wid.proc_argv_vector(p.pid) is not None:
                break
            time.sleep(0.02)
        return p

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.tmp = Path(self._t.name)
        (self.tmp / "watch-tasks-stream.sh").write_text("#!/bin/sh\nsleep 30\n")
        (self.tmp / "observer.py").write_text("import time; time.sleep(30)\n")
        self.inbox = str(self.tmp / "deliveries" / ("d" * 32))

    def test_a_real_watcher_invocation_is_true_with_its_inbox(self):
        p = self._spawn(["bash", str(self.tmp / "watch-tasks-stream.sh"), self.inbox])
        if wid.proc_argv_vector(p.pid) is None:
            self.skipTest("no authoritative argv read on this platform")
        got = wid.inspect_pid(p.pid)
        self.assertEqual((got.observed, got.watcher, got.operands), (True, True, [self.inbox]), got)

    def test_a_real_observer_mentioning_the_script_is_false(self):
        p = self._spawn([sys.executable, str(self.tmp / "observer.py"),
                         "src/watch-tasks-stream.sh", self.inbox])
        got = wid.inspect_pid(p.pid)
        self.assertEqual((got.observed, got.watcher), (True, False), got)

    def test_this_test_process_is_not_a_watcher(self):
        got = wid.inspect_pid(os.getpid())
        self.assertEqual((got.observed, got.watcher), (True, False), got)


class TestHealthCheckDelegates(unittest.TestCase):
    """The health check's private names are wrappers: they read the argv vector
    it binds at call time, and no second copy of the anchor exists in it."""

    def test_the_wrapper_honours_a_rebound_argv_reader(self):
        saved = hc._proc_argv_vector
        hc._proc_argv_vector = vector("bash", "/repo/src/watch-tasks-stream.sh", INBOX)
        try:
            self.assertIs(hc._is_watcher_argv(OBSERVER, 4242), True)
            hc._proc_argv_vector = vector("python3", "observer.py", "src/watch-tasks-stream.sh")
            self.assertIs(hc._is_watcher_argv("bash src/watch-tasks-stream.sh", 4242), False)
        finally:
            hc._proc_argv_vector = saved

    def test_the_tree_walk_uses_the_shared_verdict(self):
        saved = hc._proc_argv_vector
        hc._proc_argv_vector = unreadable
        try:
            trees = hc._watcher_trees(f"  100 1 {OBSERVER}\n  200 1 bash src/watch-tasks-stream.sh\n")
        finally:
            hc._proc_argv_vector = saved
        self.assertEqual(set(trees), {"200"})

    def test_no_private_copy_of_the_anchor(self):
        anchor = re.compile(r"watch-tasks-stream\\\.sh")
        # Control: the pattern fires on the owner, so an absence below is measured.
        self.assertTrue(anchor.search((ROOT / "src" / "watcher_identity.py").read_text()))
        self.assertIsNone(anchor.search((ROOT / "src" / "health-check.py").read_text()),
                          "health-check.py carries its own copy of the watcher regex")
        for name in ("_is_watcher_argv", "_ps_watcher_index", "_watcher_trees"):
            src = (ROOT / "src" / "health-check.py").read_text()
            body = src[src.index(f"def {name}("):]
            body = body[:body.index("\ndef ", 1)]
            self.assertIn("watcher_identity.", body, f"{name} does not delegate")


WORKER_INBOX = "/ws/deliveries/" + "w" * 32
CORE_SESSION_ARGS = ["bash", "src/watch-tasks-stream.sh", "--role", "session", "--inbox", INBOX]
CORE_SESSION_FLAT = "bash src/watch-tasks-stream.sh --role session --inbox " + INBOX
WORKER_SESSION_ARGS = ["bash", "src/watch-tasks-stream.sh", WORKER_INBOX,
                       "--role", "session", "--inbox", WORKER_INBOX]
WORKER_SESSION_FLAT = "bash src/watch-tasks-stream.sh " + WORKER_INBOX + " --role session --inbox " + WORKER_INBOX


def vector_for(pid_map):
    return lambda pid: pid_map.get(str(pid))


class TestWatcherRoleAndInboxOperands(unittest.TestCase):
    def test_role_flag_and_equals_form(self):
        self.assertEqual(wid.watcher_role(["--role", "session"]), "session")
        self.assertEqual(wid.watcher_role(["--role=session"]), "session")
        self.assertIsNone(wid.watcher_role([]))
        self.assertIsNone(wid.watcher_role(None))
        self.assertIsNone(wid.watcher_role(["--role"]))  # dangling flag, no value

    def test_inbox_flag_and_equals_form(self):
        self.assertEqual(wid.watcher_inbox(["--inbox", INBOX]), INBOX)
        self.assertEqual(wid.watcher_inbox([f"--inbox={INBOX}"]), INBOX)
        self.assertIsNone(wid.watcher_inbox(["--role", "session"]))


class TestRolePresentInboxAware(unittest.TestCase):
    """role_present() closes #4477's host-wide-not-inbox-scoped gap: a
    same-role watcher for a DIFFERENT inbox must not satisfy this caller's
    query, or one instance's crash reads as another instance's coverage."""

    def test_a_session_watcher_for_a_different_inbox_does_not_satisfy_the_query(self):
        ps_output = f"  100 1 {CORE_SESSION_FLAT}\n"
        vec = vector_for({"100": CORE_SESSION_ARGS})
        self.assertIs(wid.role_present("session", inbox=WORKER_INBOX,
                                       ps_output=ps_output, argv_vector=vec), False)

    def test_a_session_watcher_for_the_same_inbox_satisfies_the_query(self):
        ps_output = f"  100 1 {CORE_SESSION_FLAT}\n"
        vec = vector_for({"100": CORE_SESSION_ARGS})
        self.assertIs(wid.role_present("session", inbox=INBOX,
                                       ps_output=ps_output, argv_vector=vec), True)

    def test_omitting_inbox_preserves_the_old_host_wide_behavior(self):
        ps_output = f"  100 1 {CORE_SESSION_FLAT}\n"
        vec = vector_for({"100": CORE_SESSION_ARGS})
        self.assertIs(wid.role_present("session", ps_output=ps_output, argv_vector=vec), True)

    def test_two_instances_each_query_sees_only_its_own_inbox(self):
        ps_output = f"  100 1 {CORE_SESSION_FLAT}\n  200 1 {WORKER_SESSION_FLAT}\n"
        vec = vector_for({"100": CORE_SESSION_ARGS, "200": WORKER_SESSION_ARGS})
        self.assertIs(wid.role_present("session", inbox=INBOX, ps_output=ps_output, argv_vector=vec), True)
        self.assertIs(wid.role_present("session", inbox=WORKER_INBOX, ps_output=ps_output, argv_vector=vec), True)
        third_inbox = "/ws/deliveries/" + "z" * 32
        self.assertIs(wid.role_present("session", inbox=third_inbox, ps_output=ps_output, argv_vector=vec), False)

    def test_a_dead_instances_crash_does_not_read_as_a_live_peers_coverage(self):
        """The exact #4477 scenario: core's session watcher is down but a
        worker's is up -- core's own inbox-scoped query must say False, not
        be satisfied by the worker's unrelated, still-live watcher."""
        ps_output = f"  200 1 {WORKER_SESSION_FLAT}\n"
        vec = vector_for({"200": WORKER_SESSION_ARGS})
        self.assertIs(wid.role_present("session", inbox=INBOX, ps_output=ps_output, argv_vector=vec), False)

    def test_no_role_present_at_all_is_false_not_none(self):
        ps_output = f"  100 1 {OBSERVER}\n"
        got = wid.role_present("session", inbox=INBOX, ps_output=ps_output, argv_vector=unreadable)
        self.assertIs(got, False)

    def test_unobservable_ps_snapshot_is_none_not_false(self):
        def raising_run(*_a, **_k):
            raise FileNotFoundError("ps")
        self.assertIsNone(wid.role_present("session", inbox=INBOX, run=raising_run))

    def test_a_ps_that_ran_and_answered_non_zero_is_none_not_false(self):
        """subprocess.run() does not raise on a non-zero exit -- a `ps` that
        ran and failed must not read as a clean empty scan (real bug: found
        via a live regression that expected `unknown`, got `no`)."""
        def failing_run(*_a, **_k):
            return subprocess.CompletedProcess(["ps"], 1, "", "")
        self.assertIsNone(wid.role_present("session", inbox=INBOX, run=failing_run))

    def _undecidable_per_pid_run(self, *_a, **_k):
        """Simulates `ps -p <pid>` succeeding with a flattened, multi-token
        line that classify_argv cannot decide without an authoritative vector
        (matches the boundary but has trailing tokens past the script path)."""
        return subprocess.CompletedProcess(["ps"], 0, CORE_SESSION_FLAT + "\n", "")

    def test_an_undecidable_candidate_with_no_other_match_is_none_not_false(self):
        """A pid the tree walk flagged watcher-shaped, whose authoritative
        per-pid argv can't be read, must not be silently skipped into a
        confident False -- it might have been the match."""
        ps_output = f"  100 1 {CORE_SESSION_FLAT}\n"
        self.assertIsNone(wid.role_present("session", inbox=INBOX,
                                           ps_output=ps_output, argv_vector=unreadable,
                                           run=self._undecidable_per_pid_run))

    def test_an_undecidable_candidate_does_not_hide_a_real_match_elsewhere(self):
        """One tree undecidable, a second tree a genuine match: the real
        True answer still wins over the undecidable one."""
        ps_output = f"  100 1 {CORE_SESSION_FLAT}\n  200 1 {WORKER_SESSION_FLAT}\n"
        def run(argv, **kw):
            pid = argv[argv.index("-p") + 1] if "-p" in argv else None
            if pid == "200":
                return subprocess.CompletedProcess(argv, 0, WORKER_SESSION_FLAT + "\n", "")
            return self._undecidable_per_pid_run(argv, **kw)
        vec = vector_for({"200": WORKER_SESSION_ARGS})  # 100 stays unreadable
        self.assertIs(wid.role_present("session", inbox=WORKER_INBOX,
                                       ps_output=ps_output, argv_vector=vec, run=run), True)


class TestRolePresentCli(unittest.TestCase):
    def _run(self, argv, **patches):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            with mock.patch.multiple(wid, **patches) if patches else contextlib.nullcontext():
                rc = wid.main(argv)
        return rc, buf.getvalue().splitlines(), err.getvalue()

    def test_yes(self):
        rc, out, _ = self._run(["role-present", "session", "--inbox", INBOX],
                               role_present=lambda *_a, **_k: True)
        self.assertEqual((rc, out[0]), (0, "yes"))

    def test_no(self):
        rc, out, _ = self._run(["role-present", "session", "--inbox", INBOX],
                               role_present=lambda *_a, **_k: False)
        self.assertEqual((rc, out[0]), (0, "no"))

    def test_unknown_exits_2_and_never_prints_no(self):
        rc, out, _ = self._run(["role-present", "session"], role_present=lambda *_a, **_k: None)
        self.assertEqual((rc, out[0]), (2, "unknown"))

    def test_inbox_is_forwarded_to_role_present(self):
        seen = {}
        def fake(role, inbox=None, **_k):
            seen["role"], seen["inbox"] = role, inbox
            return True
        self._run(["role-present", "session", "--inbox", INBOX], role_present=fake)
        self.assertEqual(seen, {"role": "session", "inbox": INBOX})

    def test_missing_inbox_forwards_none(self):
        seen = {}
        def fake(role, inbox=None, **_k):
            seen["inbox"] = inbox
            return True
        self._run(["role-present", "session"], role_present=fake)
        self.assertIsNone(seen["inbox"])

    def test_usage_error_on_missing_role(self):
        rc, out, err = self._run(["role-present"])
        self.assertEqual(rc, 64)
        self.assertIn("usage", err)
        self.assertEqual(out, [])


class TestCliVerdicts(unittest.TestCase):
    """The shell adapter reads one word: each verdict must be reachable, and an
    unobservable pid must never print the word that licenses cleanup."""

    def _run(self, argv, **patches):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            with mock.patch.multiple(wid, **patches) if patches else contextlib.nullcontext():
                rc = wid.main(argv)
        return rc, buf.getvalue().splitlines(), err.getvalue()

    def test_watcher(self):
        seen = wid.Inspection(True, True, ["/inbox"], "bash src/watch-tasks-stream.sh /inbox", "")
        rc, out, _ = self._run(["123"], inspect_pid=lambda *_a, **_k: seen)
        self.assertEqual((rc, out[0]), (0, "watcher"))

    def test_not_watcher(self):
        seen = wid.Inspection(True, False, None, "python3 observer.py", "")
        rc, out, _ = self._run(["123"], inspect_pid=lambda *_a, **_k: seen)
        self.assertEqual((rc, out[0]), (0, "not-watcher"))

    def test_gone_is_dead_not_unknown(self):
        # ps proved nothing AND the pid is gone: cleanup is licensed.
        seen = wid.Inspection(False, None, None, "", "ps answered rc 1")
        def _kill(_pid, _sig):
            raise ProcessLookupError
        rc, out, _ = self._run(["123"], inspect_pid=lambda *_a, **_k: seen,
                               os=mock.Mock(kill=_kill))
        self.assertEqual((rc, out[0]), (0, "dead"))

    def test_live_but_unobservable_is_unknown(self):
        # The hazard: a LIVE watcher whose ps cannot be read must license nothing.
        seen = wid.Inspection(False, None, None, "", "ps answered rc 1")
        rc, out, _ = self._run(["123"], inspect_pid=lambda *_a, **_k: seen,
                               os=mock.Mock(kill=lambda *_a: None))
        self.assertEqual((rc, out[0]), (2, "unknown"))

    def test_undecidable_argv_is_unknown(self):
        seen = wid.Inspection(True, None, None, "bash -c something", "argv could not be decided")
        rc, out, _ = self._run(["123"], inspect_pid=lambda *_a, **_k: seen)
        self.assertEqual((rc, out[0]), (2, "unknown"))

    def test_exists_but_unprobeable_is_unknown(self):
        # EPERM: the pid exists, so `dead` would be a lie; we still know nothing.
        def _kill(_pid, _sig):
            raise PermissionError
        seen = wid.Inspection(False, None, None, "", "ps answered rc 1")
        rc, out, _ = self._run(["123"], inspect_pid=lambda *_a, **_k: seen,
                               os=mock.Mock(kill=_kill))
        self.assertEqual((rc, out[0]), (2, "unknown"))

    def test_unparseable_pid_is_unknown(self):
        seen = wid.Inspection(False, None, None, "", "ps answered rc 1")
        rc, out, _ = self._run(["not-a-pid"], inspect_pid=lambda *_a, **_k: seen)
        self.assertEqual((rc, out[0]), (2, "unknown"))

    def test_usage_error(self):
        rc, out, err = self._run([])
        self.assertEqual(rc, 64)
        self.assertIn("usage", err)
        self.assertEqual(out, [])


class TestRolePresentEdges(unittest.TestCase):
    """The branches a happy-path snapshot never reaches."""

    def test_watcher_inbox_with_no_operands_is_none(self):
        self.assertIsNone(wid.watcher_inbox([]))
        self.assertIsNone(wid.watcher_inbox(None))
        self.assertIsNone(wid.watcher_inbox(["--inbox="]))

    def test_a_successful_ps_run_is_read_from_its_stdout(self):
        def run(*_a, **_k):
            return subprocess.CompletedProcess(["ps"], 0, f"  100 1 {CORE_SESSION_FLAT}\n", "")
        vec = vector_for({"100": CORE_SESSION_ARGS})
        self.assertIs(wid.role_present("session", inbox=INBOX, run=run, argv_vector=vec), True)

    def test_short_lines_and_this_process_are_skipped(self):
        me = os.getpid()
        ps_output = f"  {me} 1 {CORE_SESSION_FLAT}\nPID PPID\n\n"
        vec = vector_for({str(me): CORE_SESSION_ARGS})
        self.assertIs(wid.role_present("session", inbox=INBOX, ps_output=ps_output, argv_vector=vec), False)

    def test_a_caller_supplied_is_watcher_veto_skips_the_line(self):
        ps_output = f"  100 1 {CORE_SESSION_FLAT}\n"
        vec = vector_for({"100": CORE_SESSION_ARGS})
        got = wid.role_present("session", inbox=INBOX, ps_output=ps_output, argv_vector=vec,
                               is_watcher=lambda _argv, _pid: False)
        self.assertIs(got, False)

    def test_a_watcher_with_another_role_does_not_satisfy_the_query(self):
        ps_output = f"  100 1 {CORE_SESSION_FLAT}\n"
        vec = vector_for({"100": CORE_SESSION_ARGS})
        self.assertIs(wid.role_present("standby", inbox=INBOX, ps_output=ps_output, argv_vector=vec), False)


class TestRolePresentCliForms(unittest.TestCase):
    def _run(self, args, verdict):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(wid, "role_present", return_value=verdict) as rp, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = wid.main(args)
        return rc, out.getvalue().strip(), err.getvalue(), rp

    def test_inbox_equals_form_is_parsed(self):
        rc, out, _, rp = self._run(["role-present", "session", f"--inbox={INBOX}"], True)
        self.assertEqual((rc, out), (0, "yes"))
        rp.assert_called_once_with("session", INBOX, ready=False, state_dir=None)

    def test_inbox_flag_form_is_parsed(self):
        rc, out, _, rp = self._run(["role-present", "session", "--inbox", INBOX], False)
        self.assertEqual((rc, out), (0, "no"))
        rp.assert_called_once_with("session", INBOX, ready=False, state_dir=None)

    def test_a_missing_role_is_a_usage_error(self):
        rc, _, err, rp = self._run(["role-present"], False)
        self.assertEqual(rc, 64)
        self.assertIn("usage", err)
        rp.assert_not_called()

    def test_an_unobservable_snapshot_prints_unknown_and_exits_2(self):
        rc, out, _, _ = self._run(["role-present", "session"], None)
        self.assertEqual(rc, 2)
        self.assertEqual(out.splitlines()[0], "unknown")

    def test_an_unknown_option_is_a_usage_error(self):
        rc, out, err, rp = self._run(["role-present", "session", "--nope"], False)
        self.assertEqual(rc, 64)
        self.assertIn("usage", err)
        rp.assert_not_called()




class TestReadyGate(unittest.TestCase):
    """A session watcher counts under `ready` only once the inbox's sentinel names it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.ps = f"  100 1 {CORE_SESSION_FLAT}\n"
        self.vec = vector_for({"100": CORE_SESSION_ARGS})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_without_ready_the_process_alone_counts(self):
        self.assertIs(wid.role_present("session", inbox=INBOX, ps_output=self.ps,
                                       argv_vector=self.vec, state_dir=self.state), True)

    def test_ready_with_no_sentinel_is_a_decided_no(self):
        self.assertIs(wid.role_present("session", inbox=INBOX, ps_output=self.ps,
                                       argv_vector=self.vec, ready=True, state_dir=self.state), False)

    def test_ready_once_the_sentinel_names_the_pid(self):
        with open(os.path.join(self.state, "watch-tasks-stream.pid"), "w") as fh:
            fh.write("100\n")
        self.assertIs(wid.role_present("session", inbox=INBOX, ps_output=self.ps,
                                       argv_vector=self.vec, ready=True, state_dir=self.state), True)

    def test_a_sentinel_naming_another_pid_is_not_ready(self):
        with open(os.path.join(self.state, "watch-tasks-stream.pid"), "w") as fh:
            fh.write("200\n")
        self.assertIs(wid.role_present("session", inbox=INBOX, ps_output=self.ps,
                                       argv_vector=self.vec, ready=True, state_dir=self.state), False)

    def test_an_unreadable_sentinel_is_not_ready(self):
        with open(os.path.join(self.state, "watch-tasks-stream.pid"), "w") as fh:
            fh.write("not-a-pid\n")
        self.assertIs(wid.role_present("session", inbox=INBOX, ps_output=self.ps,
                                       argv_vector=self.vec, ready=True, state_dir=self.state), False)

    def test_ready_with_no_state_dir_named_is_a_decided_no(self):
        self.assertIs(wid.role_present("session", inbox=INBOX, ps_output=self.ps,
                                       argv_vector=self.vec, ready=True), False)

    def test_cli_ready_takes_the_state_dir_from_the_caller(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(wid, "role_present", return_value=True) as rp, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = wid.main(["role-present", "session", "--inbox", INBOX, "--ready", self.state])
            rc_bare = wid.main(["role-present", "session", "--ready"])
        rp.assert_called_once_with("session", INBOX, ready=True, state_dir=self.state)
        self.assertEqual((rc, out.getvalue().strip()), (0, "yes"))
        self.assertEqual(rc_bare, 64)
        self.assertIn("usage", err.getvalue())


class TestStandbyPresent(unittest.TestCase):
    """The external standby is an UNTAGGED watcher; only its positional operand names its inbox."""

    def test_positional_inbox_skips_flags_and_their_values(self):
        self.assertEqual(wid.positional_inbox([INBOX, "--role", "session", "--inbox", INBOX]), INBOX)
        self.assertEqual(wid.positional_inbox(["--role", "session", INBOX]), INBOX)
        self.assertIsNone(wid.positional_inbox(["--role", "session", "--inbox", INBOX]))
        self.assertIsNone(wid.positional_inbox([]))
        self.assertIsNone(wid.positional_inbox(None))

    def test_an_untagged_watcher_on_the_inbox_is_a_standby(self):
        ps = f"  100 1 {GENUINE}\n"
        vec = vector_for({"100": ["/bin/bash", "/repo/src/watch-tasks-stream.sh", INBOX]})
        self.assertIs(wid.standby_present(INBOX, ps_output=ps, argv_vector=vec), True)

    def test_a_session_watcher_is_not_a_standby(self):
        ps = f"  100 1 {CORE_SESSION_FLAT}\n"
        vec = vector_for({"100": CORE_SESSION_ARGS})
        self.assertIs(wid.standby_present(INBOX, ps_output=ps, argv_vector=vec), False)

    def test_another_inbox_is_not_this_standby(self):
        other = "/ws/deliveries/" + "e" * 32
        ps = f"  100 1 bash src/watch-tasks-stream.sh {other}\n"
        vec = vector_for({"100": ["/bin/bash", "/repo/src/watch-tasks-stream.sh", other]})
        self.assertIs(wid.standby_present(INBOX, ps_output=ps, argv_vector=vec), False)

    def test_an_undecidable_line_is_unknown(self):
        ps = "  100 1 bash /some path/watch-tasks-stream.sh extra\n"
        self.assertIsNone(wid.standby_present(INBOX, ps_output=ps, argv_vector=vector_for({})))

    def test_an_unobservable_snapshot_is_unknown(self):
        def run(*_a, **_k):
            return subprocess.CompletedProcess(["ps"], 1, "", "")
        self.assertIsNone(wid.standby_present(INBOX, run=run))

    def test_cli_standby_present(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(wid, "standby_present", return_value=False) as sp, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = wid.main(["standby-present", "--inbox", INBOX])
            rc_usage = wid.main(["standby-present"])
        sp.assert_called_once_with(INBOX)
        self.assertEqual((rc, out.getvalue().strip()), (0, "no"))
        self.assertEqual(rc_usage, 64)
        self.assertIn("usage", err.getvalue())




class TestReadyGateErrorPaths(unittest.TestCase):
    """Every fallback in the sentinel read and the CLI equals forms, with real inputs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.ps = f"  100 1 {CORE_SESSION_FLAT}\n"
        self.vec = vector_for({"100": CORE_SESSION_ARGS})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sentinel(self, body):
        with open(os.path.join(self.state, "watch-tasks-stream.pid"), "w") as fh:
            fh.write(body)

    def test_an_empty_sentinel_is_not_a_stamp(self):
        self._sentinel("")
        self.assertFalse(wid.sentinel_names_pid(100, self.state))

    def test_a_sentinel_that_cannot_be_read_is_not_a_stamp(self):
        self._sentinel("100\n")
        os.chmod(os.path.join(self.state, "watch-tasks-stream.pid"), 0)
        try:
            self.assertFalse(wid.sentinel_names_pid(100, self.state))
        finally:
            os.chmod(os.path.join(self.state, "watch-tasks-stream.pid"), 0o644)

    def test_a_missing_state_dir_is_not_a_stamp(self):
        self.assertFalse(wid.sentinel_names_pid(100, os.path.join(self.tmp, "absent")))

    def test_no_pid_or_no_dir_is_not_a_stamp(self):
        self.assertFalse(wid.sentinel_names_pid(None, self.state))
        self.assertFalse(wid.sentinel_names_pid(100, None))

    def test_a_broken_sentinel_helper_is_not_a_stamp(self):
        self._sentinel("100\n")
        import util_paths
        with mock.patch.object(util_paths, "watcher_sentinel_paths", side_effect=RuntimeError("no")):
            self.assertFalse(wid.sentinel_names_pid(100, self.state))

    def test_cli_ready_equals_form(self):
        self._sentinel("100\n")
        out = io.StringIO()
        with mock.patch.object(wid, "role_present", return_value=True) as rp, \
                contextlib.redirect_stdout(out):
            rc = wid.main(["role-present", "session", f"--inbox={INBOX}", f"--ready={self.state}"])
        rp.assert_called_once_with("session", INBOX, ready=True, state_dir=self.state)
        self.assertEqual((rc, out.getvalue().strip()), (0, "yes"))

    def test_cli_ready_equals_with_no_value_is_not_ready(self):
        with mock.patch.object(wid, "role_present", return_value=False) as rp, \
                contextlib.redirect_stdout(io.StringIO()):
            wid.main(["role-present", "session", "--ready="])
        rp.assert_called_once_with("session", None, ready=False, state_dir=None)


class TestStandbyPresentEdges(unittest.TestCase):
    def test_positional_inbox_skips_a_lone_flag_and_a_flag_value(self):
        self.assertEqual(wid.positional_inbox(["--verbose", INBOX]), INBOX)
        self.assertEqual(wid.positional_inbox(["--role", "session", "--inbox", INBOX, "/other"]), "/other")

    def test_a_ps_that_raises_is_unknown(self):
        def run(*_a, **_k):
            raise OSError("no ps")
        self.assertIsNone(wid.standby_present(INBOX, run=run))

    def test_a_successful_ps_is_read_from_its_stdout(self):
        def run(*_a, **_k):
            return subprocess.CompletedProcess(["ps"], 0, f"  100 1 {GENUINE}\n", "")
        vec = vector_for({"100": ["/bin/bash", "/repo/src/watch-tasks-stream.sh", INBOX]})
        self.assertIs(wid.standby_present(INBOX, run=run, argv_vector=vec), True)

    def test_short_lines_this_process_and_non_watchers_are_skipped(self):
        ps = f"  1 0\n  {os.getpid()} 1 {GENUINE}\n  200 1 python3 something.py\n"
        vec = vector_for({"200": ["python3", "something.py"]})
        self.assertIs(wid.standby_present(INBOX, ps_output=ps, argv_vector=vec), False)

    def test_an_undecidable_line_for_another_inbox_is_skipped(self):
        other = "/ws/deliveries/" + "e" * 32
        ps = f"  100 1 bash /some path/watch-tasks-stream.sh --inbox {other}\n"
        self.assertIs(wid.standby_present(INBOX, ps_output=ps, argv_vector=vector_for({})), False)

    def test_an_untagged_watcher_on_another_inbox_is_skipped(self):
        other = "/ws/deliveries/" + "e" * 32
        ps = f"  100 1 bash src/watch-tasks-stream.sh {other}\n  101 1 {GENUINE}\n"
        vec = vector_for({"100": ["/bin/bash", "/repo/src/watch-tasks-stream.sh", other],
                          "101": ["/bin/bash", "/repo/src/watch-tasks-stream.sh", INBOX]})
        self.assertIs(wid.standby_present(INBOX, ps_output=ps, argv_vector=vec), True)

    def test_cli_standby_present_equals_form_and_unknown(self):
        out = io.StringIO()
        with mock.patch.object(wid, "standby_present", return_value=None) as sp, \
                contextlib.redirect_stdout(out):
            rc = wid.main(["standby-present", f"--inbox={INBOX}"])
        sp.assert_called_once_with(INBOX)
        self.assertEqual(rc, 2)
        self.assertEqual(out.getvalue().splitlines()[0], "unknown")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(wid.main(["standby-present", "--inbox="]), 64)
        self.assertIn("usage", err.getvalue())


class TestInboxHolders(unittest.TestCase):
    """inbox_holders() is presence, tagged or not, ready or not: what the watcher's own
    startup asks before it doubles an inbox. Readiness stays the supervisor's question,
    and an undecidable line is counted, never reported as a holder."""

    def test_no_watcher_is_an_empty_list_of_holders(self):
        ps = "  100 1 python3 something-else\n"
        seen = wid.inbox_holders(INBOX, ps_output=ps, argv_vector=vector_for({}))
        self.assertEqual((seen.observed, seen.holders, seen.undecided), (True, [], 0))

    def test_a_tagged_session_watcher_is_a_session_holder(self):
        ps = f"  100 1 {CORE_SESSION_FLAT}\n"
        vec = vector_for({"100": CORE_SESSION_ARGS})
        self.assertEqual(wid.inbox_holders(INBOX, ps_output=ps, argv_vector=vec).holders,
                         [(100, "session")])

    def test_an_untagged_watcher_is_an_untagged_holder(self):
        ps = f"  100 1 {GENUINE}\n"
        vec = vector_for({"100": ["/bin/bash", "/repo/src/watch-tasks-stream.sh", INBOX]})
        self.assertEqual(wid.inbox_holders(INBOX, ps_output=ps, argv_vector=vec).holders,
                         [(100, "untagged")])

    def test_a_standby_tag_is_a_standby_holder(self):
        args = ["bash", "src/watch-tasks-stream.sh", INBOX, "--role", "standby", "--inbox", INBOX]
        ps = f"  100 1 {' '.join(args)}\n"
        self.assertEqual(wid.inbox_holders(INBOX, ps_output=ps,
                                           argv_vector=vector_for({"100": args})).holders,
                         [(100, "standby")])

    def test_another_inbox_is_never_a_holder(self):
        other = "/ws/deliveries/" + "e" * 32
        ps = f"  100 1 bash src/watch-tasks-stream.sh {other}\n"
        vec = vector_for({"100": ["/bin/bash", "/repo/src/watch-tasks-stream.sh", other]})
        seen = wid.inbox_holders(INBOX, ps_output=ps, argv_vector=vec)
        self.assertEqual((seen.holders, seen.undecided), ([], 0))

    def test_the_caller_and_its_forked_children_are_excluded(self):
        # A command substitution inside the watcher forks a child carrying the
        # watcher's own argv; neither may read as a second holder.
        ps = f"  100 1 {CORE_SESSION_FLAT}\n  101 100 {CORE_SESSION_FLAT}\n"
        vec = vector_for({"100": CORE_SESSION_ARGS, "101": CORE_SESSION_ARGS})
        self.assertEqual(wid.inbox_holders(INBOX, exclude_pid=100, ps_output=ps,
                                           argv_vector=vec).holders, [])

    def test_an_undecidable_line_is_counted_and_is_not_a_holder(self):
        # A process that exits between the ps snapshot and the argv read is
        # undecidable; reading that as a holder leaves the inbox with no watcher.
        ps = f"  100 1 bash src/watch-tasks-stream.sh {INBOX} --role session\n"
        seen = wid.inbox_holders(INBOX, ps_output=ps, argv_vector=vector_for({}))
        self.assertEqual((seen.observed, seen.holders, seen.undecided), (True, [], 1))

    def test_an_undecidable_line_for_another_inbox_is_not_even_counted(self):
        other = "/ws/deliveries/" + "e" * 32
        ps = f"  100 1 bash src/watch-tasks-stream.sh x --inbox {other} --role session\n"
        seen = wid.inbox_holders(INBOX, ps_output=ps, argv_vector=vector_for({}))
        self.assertEqual(seen.undecided, 0)

    def test_a_decided_holder_still_reports_beside_an_undecidable_line(self):
        ps = (f"  100 1 {CORE_SESSION_FLAT}\n"
              f"  200 1 bash src/watch-tasks-stream.sh {INBOX} --role session\n")
        seen = wid.inbox_holders(INBOX, ps_output=ps, argv_vector=vector_for({"100": CORE_SESSION_ARGS}))
        self.assertEqual((seen.holders, seen.undecided), ([(100, "session")], 1))

    def test_an_unobservable_ps_is_not_observed(self):
        def run(*a, **k):
            raise OSError("no ps")
        seen = wid.inbox_holders(INBOX, run=run)
        self.assertEqual((seen.observed, seen.holders), (False, []))


if __name__ == "__main__":
    unittest.main(verbosity=0)
