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

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
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


if __name__ == "__main__":
    unittest.main(verbosity=0)
