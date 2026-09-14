#!/usr/bin/env python3
"""A watcher started without the routing handler while bindings.json names a
worker answers every bound room from this core, silently (2026-09-11 and
2026-09-12: two restarts, eleven and then eight owner messages answered by
the wrong seat). The watcher must refuse loudly, name the fix, and keep an
explicit opt-out; an empty or core-only declaration still starts."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import pool_bindings_declared as pbd  # noqa: E402

W = "a" * 32


class TestHelper(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.state = Path(self._t.name)

    def _write(self, text):
        (self.state / "bindings.json").write_text(text)

    def test_absent_is_none(self):
        self.assertEqual(pbd.declared(self.state)[0], False)

    def test_empty_declaration_is_none(self):
        self._write(json.dumps({"bindings": {}}))
        self.assertEqual(pbd.declared(self.state)[0], False)

    def test_core_only_is_none(self):
        self._write(json.dumps({"bindings": {"!r:x": "core", "!s:x": ["core"]}}))
        self.assertEqual(pbd.declared(self.state)[0], False)

    def test_a_worker_binding_is_declared(self):
        self._write(json.dumps({"bindings": {"!r:x": W}}))
        yes, reason = pbd.declared(self.state)
        self.assertTrue(yes)
        self.assertIn("1 room(s)", reason)

    def test_unreadable_and_misshaped_fail_closed(self):
        self._write("{not json")
        self.assertTrue(pbd.declared(self.state)[0])
        self._write(json.dumps({"bindings": ["x"]}))
        self.assertTrue(pbd.declared(self.state)[0])
        self._write(json.dumps({"rooms": {}}))
        self.assertTrue(pbd.declared(self.state)[0])

    def test_cli_exit_codes(self):
        self.assertEqual(pbd.main([str(self.state)]), 0)
        self._write(json.dumps({"bindings": {"!r:x": W}}))
        self.assertEqual(pbd.main([str(self.state)]), 1)
        self.assertEqual(pbd.main([]), 2)


class TestWatcher(unittest.TestCase):
    """The real script, own workspace and TMPDIR; only recorded pids are killed."""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name) / "ws"
        for d in ("tasks", "results", "state"):
            (self.ws / d).mkdir(parents=True)
        self.tmp = Path(self._t.name) / "tmp"
        self.tmp.mkdir()
        # Seeded before every start: the gate runs ahead of the sweep, so this
        # changes nothing for a refusal and gives a start something to announce.
        (self.ws / "tasks" / "task-seed1.txt").write_text("id: task-seed1\ntask: seed\n")
        self.procs = []
        self.addCleanup(self._kill_all)

    def _kill_all(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)

    def _start(self, inbox=None, **env_extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SUTANDO_TASK_EVENT_HANDLER", "SUTANDO_ALLOW_UNROUTED_BINDINGS")}
        env.update({"TMPDIR": str(self.tmp), "SUTANDO_WORKSPACE_DIR": str(self.ws)})
        env.update(env_extra)
        inbox = Path(inbox) if inbox else (self.ws / "tasks")
        # Files, not pipes: a started watcher never exits, so its output has to be
        # readable while it still runs, and a refusal's stderr after it is gone.
        self.out = self.tmp / f"out{len(self.procs)}"
        self.err = self.tmp / f"err{len(self.procs)}"
        with open(self.out, "w") as o, open(self.err, "w") as e:
            p = subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(inbox)],
                                 cwd=str(REPO), env=env, stdout=o, stderr=e,
                                 text=True, start_new_session=True)
        p._out, p._err = self.out, self.err
        self.procs.append(p)
        return p

    def _bind(self, target=W):
        (self.ws / "state" / "bindings.json").write_text(json.dumps({"bindings": {"!r:x": target}}))

    def _assert_refused(self, p):
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("watcher did not exit; it should have refused")
        err = p._err.read_text()
        self.assertEqual(p.returncode, 78, err)
        self.assertIn("REFUSING to start", err)
        self.assertIn("SUTANDO_TASK_EVENT_HANDLER=", err)
        self.assertIn("pool_route_handler.py", err)
        self.assertIn("SUTANDO_ALLOW_UNROUTED_BINDINGS=1", err)
        return err

    def _assert_started(self, p):
        # Proven by the work the gate lets through, not by still being alive:
        # liveness also needs fswatch, which CI lacks. The sweep precedes it.
        deadline = time.time() + 20
        while time.time() < deadline:
            if "TASK_FILE:" in p._out.read_text():
                self.assertNotIn("REFUSING to start", p._err.read_text())
                return
            if p.poll() is not None:
                break
            time.sleep(0.2)
        self.fail(f"watcher never swept the seeded task (rc={p.poll()}): "
                  f"{p._err.read_text()[-400:]}")

    def test_bindings_and_no_handler_refuse_and_name_the_fix(self):
        self._bind()
        err = self._assert_refused(self._start())
        self.assertIn("is unset", err)

    def test_a_non_executable_handler_is_named_as_such(self):
        self._bind()
        stub = self.ws / "handler.py"
        stub.write_text("#!/bin/sh\nexit 3\n")   # not chmod +x
        err = self._assert_refused(self._start(SUTANDO_TASK_EVENT_HANDLER=str(stub)))
        self.assertIn("not executable", err)

    def test_a_corrupt_declaration_refuses_too(self):
        (self.ws / "state" / "bindings.json").write_text("{not json")
        self._assert_refused(self._start())

    def test_no_declaration_starts(self):
        self._assert_started(self._start())

    def test_core_only_declaration_starts(self):
        self._bind("core")
        self._assert_started(self._start())

    def test_the_opt_out_starts_unrouted_on_purpose(self):
        self._bind()
        self._assert_started(self._start(SUTANDO_ALLOW_UNROUTED_BINDINGS="1"))

    def test_an_executable_handler_starts(self):
        self._bind()
        stub = self.ws / "handler.sh"
        stub.write_text("#!/bin/sh\nexit 3\n")
        stub.chmod(0o755)
        self._assert_started(self._start(SUTANDO_TASK_EVENT_HANDLER=str(stub)))

    def _delivery_inbox(self):
        """A worker's inbox: <ws>/deliveries/<id>, seeded so a start can announce."""
        d = self.ws / "deliveries" / W
        d.mkdir(parents=True)
        (d / "task-seed1.txt").write_text("id: task-seed1\ntask: seed\n")
        return d

    def test_a_worker_delivery_watcher_starts_under_the_same_declaration(self):
        # A worker legitimately carries no routing handler, so a correctly
        # configured multi-worker host must not trip this gate.
        self._bind()
        self._assert_started(self._start(inbox=self._delivery_inbox()))

    def test_an_unresolvable_core_inbox_refuses_rather_than_reading_as_worker(self):
        # "Cannot tell" must not share a branch with "definitely a worker".
        elsewhere = Path(self._t.name) / "no-tasks-ws"
        (elsewhere / "state").mkdir(parents=True)
        # Under the DECLARED workspace, or an absent declaration decides the start.
        (elsewhere / "state" / "bindings.json").write_text(
            json.dumps({"bindings": {"!r:x": W}}))
        self.assertFalse((elsewhere / "tasks").exists())
        self._assert_refused(self._start(SUTANDO_WORKSPACE_DIR=str(elsewhere)))

    def test_the_core_intake_still_refuses_under_that_same_setup(self):
        # Positive control: identical declaration and env, only the inbox differs,
        # or the start above is satisfied by a gate that fires nowhere.
        self._bind()
        self._delivery_inbox()
        err = self._assert_refused(self._start())
        self.assertIn("is unset", err)


if __name__ == "__main__":
    unittest.main()
