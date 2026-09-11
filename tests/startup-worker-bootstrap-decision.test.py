#!/usr/bin/env python3
"""A worker's watcher gate is about ONE instance, not about the host.

`/schedule-crons` step 1.5 gates on running watcher TREES — correct for the
core, and on a pool host always satisfied by the core's own, so a worker that
consults it answers `skip` and never starts the watcher it exists to run. This
gate reads the sentinel THIS instance stamps, resolved by the watcher's own
owner (`util_paths.watcher_sentinel_path`), and liveness arrives as a callable
so neither polarity needs a process.

Run: python3 tests/startup-worker-bootstrap-decision.test.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "skills" / "startup" / "scripts" / "worker-bootstrap.py"
sys.path.insert(0, str(GATE.parent))
sys.path.insert(0, str(REPO / "src"))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("worker_bootstrap", GATE)
wb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb)

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

    def ask(self, *, instance=WORKER, alive=lambda pid: True):
        return wb.decide(instance=instance, inbox=self.inbox,
                         workspace=str(self.ws), alive=alive)


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


class TestRefusals(Base):
    def test_a_session_with_no_instance_is_not_a_worker(self):
        self.assertEqual(self.ask(instance="")[0], "unknown")

    def test_no_inbox_is_unknown_not_start(self):
        d, _ = wb.decide(instance=WORKER, inbox="", workspace=str(self.ws))
        self.assertEqual(d, "unknown")

    def test_an_unresolvable_sentinel_is_unknown_not_start(self):
        """A duplicate watcher on one folder processes every delivery twice, so
        'could not tell' must never read as 'none running'."""
        def boom(state_dir, instance):
            raise RuntimeError("no identity here")
        d, why = wb.decide(instance=WORKER, inbox=self.inbox,
                           workspace=str(self.ws), resolve=boom)
        self.assertEqual(d, "unknown")
        self.assertIn("no identity here", why)


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


if __name__ == "__main__":
    unittest.main(verbosity=0)
