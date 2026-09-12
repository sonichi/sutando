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

    def _start(self, **env_extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SUTANDO_TASK_EVENT_HANDLER", "SUTANDO_ALLOW_UNROUTED_BINDINGS")}
        env.update({"TMPDIR": str(self.tmp), "SUTANDO_WORKSPACE_DIR": str(self.ws)})
        env.update(env_extra)
        p = subprocess.Popen(["bash", "src/watch-tasks-stream.sh", str(self.ws / "tasks")],
                             cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.procs.append(p)
        return p

    def _bind(self, target=W):
        (self.ws / "state" / "bindings.json").write_text(json.dumps({"bindings": {"!r:x": target}}))

    def _assert_refused(self, p):
        try:
            _out, err = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("watcher did not exit; it should have refused")
        self.assertEqual(p.returncode, 78, err)
        self.assertIn("REFUSING to start", err)
        self.assertIn("SUTANDO_TASK_EVENT_HANDLER=", err)
        self.assertIn("pool_route_handler.py", err)
        self.assertIn("SUTANDO_ALLOW_UNROUTED_BINDINGS=1", err)
        return err

    def _assert_started(self, p):
        # A refusal exits within a second; a started watcher is still running well after.
        deadline = time.time() + 4
        while time.time() < deadline:
            if p.poll() is not None:
                _out, err = p.communicate()
                self.fail(f"watcher exited rc={p.returncode}: {err[-400:]}")
            time.sleep(0.2)

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


if __name__ == "__main__":
    unittest.main()
