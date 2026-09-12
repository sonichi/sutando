#!/usr/bin/env python3
"""A worker's watcher gate is about ONE instance, not about the host.

`/schedule-crons` step 1.5 gates on running watcher TREES — correct for the
core, and on a pool host always satisfied by the core's own, so a worker that
consults it answers `skip` and never starts the watcher it exists to run. This
gate reads the sentinel THIS instance stamps, resolved by the watcher's own
owner (`util_paths.watcher_sentinel_path`), and liveness arrives as a callable
so neither polarity needs a process.

Run: python3 tests/skills/worker-pool/worker-bootstrap-decision.test.py
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GATE = Path(__file__).resolve().parents[3] / "skills/worker-pool/scripts" / "worker_bootstrap.py"

# As a caller outside src/ loads it: its own sys.path bootstrap is part of
# the module, and pre-inserting src/ would leave that unrun.
_spec = importlib.util.spec_from_file_location("worker_bootstrap", GATE)
wb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb)

REPO = Path(__file__).resolve().parents[3]
WORKER = "d" * 32
OTHER = "e" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        (self.ws / "state").mkdir()
        self.inbox = str(self.ws / "deliveries" / WORKER)

    def sentinel(self, instance):
        import util_paths
        return util_paths.watcher_sentinel_path(self.ws / "state", instance=instance)

    def ask(self, *, instance=WORKER, alive=lambda pid: True,
            watcher_target=None):
        """`watcher_target` defaults to "this pid watches MY inbox", which is
        what every pre-ownership test meant by a live watcher."""
        if watcher_target is None:
            watcher_target = lambda pid: self.inbox          # noqa: E731
        return wb.decide(instance=instance, inbox=self.inbox,
                         workspace=str(self.ws), alive=alive,
                         watcher_target=watcher_target)


class TestInstanceScoped(Base):
    def test_no_sentinel_of_its_own_means_start(self):
        self.assertEqual(self.ask()[0], "start")

    def test_another_instances_live_watcher_does_not_suppress_this_one(self):
        """The exact suppression the host-wide gate produces: the CORE's watcher
        is live, and the worker must still start its own."""
        self.sentinel(OTHER).write_text("4242\n")
        self.sentinel(None).write_text("4243\n")   # the canonical core's
        self.assertEqual(self.ask(alive=lambda pid: True)[0], "start")

    def test_its_own_live_watcher_means_skip(self):
        self.sentinel(WORKER).write_text("4242\n")
        self.assertEqual(self.ask(alive=lambda pid: pid == 4242)[0], "skip")


    def test_its_own_dead_sentinel_means_start(self):
        self.sentinel(WORKER).write_text("4242\n")
        decision, why = self.ask(alive=lambda pid: False)
        self.assertEqual(decision, "start")
        self.assertIn("dead", why)

    def test_two_instances_never_read_the_same_file(self):
        self.assertNotEqual(self.sentinel(WORKER), self.sentinel(OTHER))
        self.assertNotEqual(self.sentinel(WORKER), self.sentinel(None))

class TestPidLiveness(Base):
    """The real `_pid_alive`, not the injected one: every other test hands
    `decide` a fake, so the shipped liveness check would go unexercised."""
    def test_this_process_is_alive(self):
        self.assertTrue(wb._pid_alive(os.getpid()))

    def test_a_reaped_child_is_not(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(wb._pid_alive(p.pid))

    def test_a_pid_this_user_cannot_signal_still_exists(self):
        """PermissionError means "it is there and not mine" — the opposite of
        gone. Injected: a host where pid 1 IS signalable would prove nothing."""
        self.assertTrue(self._kill_raising(PermissionError))

    def test_any_other_os_error_is_not_alive(self):
        self.assertFalse(self._kill_raising(OSError("bad pid")))

    def _kill_raising(self, exc):
        def boom(pid, sig):
            raise exc
        real, wb.os.kill = wb.os.kill, boom
        try:
            return wb._pid_alive(4242)
        finally:
            wb.os.kill = real


class TestRefusals(Base):
    def test_a_session_with_no_instance_is_not_a_worker(self):
        self.assertEqual(self.ask(instance="")[0], "unknown")

    def test_no_inbox_is_unknown_not_start(self):
        d, _ = wb.decide(instance=WORKER, inbox="", workspace=str(self.ws))
        self.assertEqual(d, "unknown")

    def test_no_workspace_is_unknown_not_start(self):
        d, why = wb.decide(instance=WORKER, inbox=self.inbox, workspace="")
        self.assertEqual(d, "unknown")
        self.assertIn("workspace", why)

    def test_an_unreadable_sentinel_is_unknown_not_start(self):
        """A directory where the file should be: readable-as-absent would read
        a broken stamp as "no watcher" and start a second one."""
        self.sentinel(WORKER).mkdir(parents=True)
        d, why = self.ask()
        self.assertEqual(d, "unknown")
        self.assertIn("unreadable", why)

    def test_a_sentinel_holding_no_pid_means_start(self):
        for content in ("not-a-pid\n", "   \n"):
            with self.subTest(content=content):
                self.sentinel(WORKER).write_text(content)
                d, why = self.ask()
                self.assertEqual(d, "start")
                self.assertIn("no pid", why)

    def test_an_unresolvable_sentinel_is_unknown_not_start(self):
        """A duplicate watcher on one folder processes every delivery twice, so
        'could not tell' must never read as 'none running'."""
        def boom(state_dir, instance):
            raise RuntimeError("no identity here")
        d, why = wb.decide(instance=WORKER, inbox=self.inbox,
                           workspace=str(self.ws), resolve=boom)
        self.assertEqual(d, "unknown")
        self.assertIn("no identity here", why)


class TestMain(Base):
    """In process, so the CLI's own branches are measured and not just run."""
    def run_main(self, *args):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = wb.main(list(args))
        return rc, buf.getvalue().splitlines()

    def test_a_worker_with_no_watcher_is_told_to_start(self):
        rc, out = self.run_main("--instance", WORKER, "--inbox", self.inbox,
                                "--workspace", str(self.ws))
        self.assertEqual(rc, 0)
        self.assertEqual(out[0], "start")
        self.assertIn(WORKER, out[1])

    def test_an_unknown_exits_two(self):
        rc, out = self.run_main("--instance", "", "--inbox", self.inbox,
                                "--workspace", str(self.ws))
        self.assertEqual((rc, out[0]), (2, "unknown"))

    def test_a_loader_that_cannot_answer_leaves_the_gate_unknown(self):
        """Not `start`: a gate that cannot find the workspace cannot know
        whether this instance already has a watcher."""
        import types
        stub = types.ModuleType("workspace_default")
        def boom():
            raise RuntimeError("no workspace here")
        stub.resolve_workspace = boom
        real = sys.modules.get("workspace_default")
        sys.modules["workspace_default"] = stub
        try:
            rc, out = self.run_main("--instance", WORKER, "--inbox", self.inbox)
        finally:
            if real is None:
                del sys.modules["workspace_default"]
            else:
                sys.modules["workspace_default"] = real
        self.assertEqual((rc, out[0]), (2, "unknown"))

    def test_no_workspace_given_falls_back_to_the_loader(self):
        """Not the spawner's env var: a worker shares the host's workspace, so
        the canonical loader is the only resolution path here."""
        rc, out = self.run_main("--instance", WORKER, "--inbox", self.inbox)
        self.assertIn(out[0], ("start", "skip", "unknown"))
        self.assertIn(rc, (0, 2))


class TestCli(Base):
    def test_the_cli_reports_start_and_exits_zero(self):
        r = subprocess.run([sys.executable, str(GATE), "--instance", WORKER,
                            "--inbox", self.inbox, "--workspace", str(self.ws)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "start")

    def test_an_unknown_exits_two_so_a_caller_cannot_read_it_as_start(self):
        r = subprocess.run([sys.executable, str(GATE), "--instance", "",
                            "--inbox", self.inbox, "--workspace", str(self.ws)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout.splitlines()[0], "unknown")




class TestOwnershipIsScopedToTheInbox(Base):
    def test_a_live_pid_that_is_not_a_watcher_does_not_suppress_startup(self):
        """A recycled pid, or a stale stamp: not a watcher at all."""
        self.sentinel(WORKER).write_text("4242\n")
        d, why = self.ask(watcher_target=lambda pid: None)
        self.assertEqual(d, "start")
        self.assertIn("not a watch-tasks-stream.sh", why)

    def test_another_workers_watcher_on_the_same_pid_does_not_count_as_mine(self):
        """The reviewer's case: pid reuse by ANOTHER pool/core watcher. Being a
        watcher is not being THIS worker's watcher; treating it as one strands
        every task assigned to this inbox."""
        self.sentinel(WORKER).write_text("4242\n")
        d, why = self.ask(watcher_target=lambda pid: str(self.ws / "deliveries" / OTHER))
        self.assertEqual(d, "start")
        self.assertIn(OTHER, why)
        self.assertIn("not this worker's inbox", why)

    def test_a_watcher_whose_inbox_is_not_visible_is_unknown_not_skip(self):
        """Inconclusive ownership is its own answer: starting risks a second
        watcher on one inbox (every task twice), skipping risks none at all."""
        self.sentinel(WORKER).write_text("4242\n")
        d, why = self.ask(watcher_target=lambda pid: "")
        self.assertEqual(d, "unknown")
        self.assertIn("not visible", why)

    def test_this_workers_own_watcher_still_means_skip(self):
        self.sentinel(WORKER).write_text("4242\n")
        d, why = self.ask(watcher_target=lambda pid: self.inbox)
        self.assertEqual(d, "skip")
        self.assertIn(self.inbox, why)

    def test_the_same_inbox_by_another_name_is_still_mine(self):
        """/tmp and /private/tmp name one directory; a path-string comparison
        would read this host's own watcher as a stranger's."""
        self.sentinel(WORKER).write_text("4242\n")
        d, _ = self.ask(watcher_target=lambda pid: self.inbox + "/")
        self.assertEqual(d, "skip")

    def test_ownership_is_not_consulted_for_a_dead_pid(self):
        self.sentinel(WORKER).write_text("4242\n")
        asked = []
        d, _ = self.ask(alive=lambda pid: False,
                        watcher_target=lambda pid: asked.append(pid) or "")
        self.assertEqual(d, "start")
        self.assertEqual(asked, [])

    def test_the_shipped_probe_rejects_this_non_watcher_process(self):
        """The real probe, not a double: this process is alive and is not a
        watcher, so it must not be read as one."""
        self.assertIsNone(wb._watcher_target(os.getpid()))


class TestTheShippedStartupNamesTheInbox(Base):
    """F1: the ownership check reads the watched inbox from argv, so the
    instruction that starts a worker's watcher has to put it there."""

    def test_the_worker_startup_step_passes_the_inbox(self):
        """Not "the line mentions the variable" — the shipped COMMAND has to
        name the inbox, read by the same parser the ownership check uses."""
        skill = (REPO / "skills" / "startup" / "SKILL.md").read_text(encoding="utf-8")
        line = next(ln for ln in skill.splitlines()
                    if "watch-tasks-stream.sh" in ln and "On `start` only" in ln)
        m = re.search(r"`command:\s*'([^']+)'`", line)
        self.assertIsNotNone(m, f"no quoted `command:` in the startup step: {line[:120]}")
        self.assertNotEqual(
            wb._target_from_argv(m.group(1)), "",
            "the shipped worker startup names no inbox in the watcher command, so "
            "decide() reads `unknown` forever and the worker never starts one")

    def test_a_watcher_started_that_way_is_recognised_as_this_workers(self):
        """The argv the instruction produces, parsed by the shipped probe."""
        argv = f'bash src/watch-tasks-stream.sh {self.inbox}'
        self.assertEqual(wb._target_from_argv(argv), self.inbox)

    def test_an_argv_without_an_inbox_is_still_unknown(self):
        self.assertEqual(wb._target_from_argv("bash src/watch-tasks-stream.sh"), "")

    def test_a_spaced_inbox_survives_the_flattened_argv(self):
        spaced = "/Users/me/Library/Application Support/ws/deliveries/own"
        self.assertEqual(
            wb._target_from_argv(f"bash src/watch-tasks-stream.sh {spaced}"), spaced)

    def test_a_spaced_inbox_is_still_this_workers_own(self):
        spaced = str(self.ws / "Application Support" / "deliveries" / WORKER)
        self.sentinel(WORKER).write_text("4242\n")
        decision, _ = wb.decide(
            instance=WORKER, inbox=spaced, workspace=str(self.ws),
            alive=lambda pid: True,
            watcher_target=lambda pid: wb._target_from_argv(
                f"bash src/watch-tasks-stream.sh {spaced}"))
        self.assertEqual(decision, "skip")

if __name__ == "__main__":
    unittest.main(verbosity=0)
