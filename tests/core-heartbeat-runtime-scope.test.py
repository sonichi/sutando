#!/usr/bin/env python3
"""Real resolver/writer against synthetic command responses; no live PID probes."""
import contextlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
heartbeat = importlib.import_module("core_heartbeat")
config = importlib.import_module("sutando_config")


class RuntimeScope(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket = str(self.root / "fixture.sock")
        self.session = "fixture-core"
        self.runtime = "codex"
        self.present = True
        self.panes = "4242\n"
        self.pane_argv = "codex"
        self.foreign_argv = "claude --name fixture-core"
        self.foreign_present = True
        self.runtime_after_sweep = None
        self.pane_result = 0
        self.calls = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in [("CORES_DIR", self.root), ("_SIGNALLED", False)]:
            self.stack.enter_context(patch.object(heartbeat, name, value))
        self.stack.enter_context(patch.dict(os.environ, {"TMUX": ""}))
        for name, effect in [("_tmux", self.tmux), ("core_session", lambda: self.session),
                             ("_socket_path", lambda: self.socket),
                             ("_alive_path", lambda: self.root / "fixture.alive"),
                             ("_hostname", lambda: "fixture"),
                             ("_locality", lambda: {"kind": "local", "host": "fixture"}),
                             ("_tmux_backend", lambda **kwargs: {})]:
            self.stack.enter_context(patch.object(heartbeat, name, side_effect=effect))
        self.stack.enter_context(patch.object(config, "resolve_core_runtime", side_effect=lambda: self.runtime))
        self.stack.enter_context(patch.object(heartbeat.subprocess, "run", side_effect=self.command))

    def tmux(self, socket, *args):
        self.assertEqual(socket, self.socket)
        self.calls.append(("tmux", *args))
        if args[0] == "has-session":
            self.assertEqual(args[1:], ("-t", "=" + self.session))
            return subprocess.CompletedProcess(args, 0 if self.present else 1, "", "can't find session")
        if args[0] == "show-environment":
            self.assertEqual(args[1:], ("-t", "=" + self.session, "SUTANDO_CORE_RUNTIME"))
            return subprocess.CompletedProcess(args, 0 if self.runtime else 1,
                                               "SUTANDO_CORE_RUNTIME=" + (self.runtime or ""), "")
        if args[0] == "list-panes":
            self.assertEqual(args[1:], ("-s", "-t", "=" + self.session, "-F", "#{pane_pid}"))
            if self.pane_result is None:
                return None
            return subprocess.CompletedProcess(args, self.pane_result, self.panes, "")
        self.fail("Unexpected command edge: " + args[0])

    def command(self, args, **kwargs):
        self.calls.append(tuple(args))
        if args == ["pgrep", "-x", "claude"]:
            if self.runtime_after_sweep is not None:
                self.runtime = self.runtime_after_sweep
            return subprocess.CompletedProcess(args, 0 if self.foreign_present else 1, "9191\n", "")
        if args[:3] == ["ps", "-o", "args="] and args[3] == "-p":
            self.assertIn(args[4], ["4242", "9191"])
            return subprocess.CompletedProcess(args, 0,
                                               self.pane_argv if args[4] == "4242" else self.foreign_argv, "")
        self.fail("Unexpected process edge")

    def resolve(self):
        return heartbeat.core_pid(self.socket, self.session)

    def assert_no_claude_sweep(self):
        self.assertFalse(any(call[0] in ["pgrep", "ps"] for call in self.calls))

    def test_codex_ignores_same_named_foreign_claude(self):
        self.assertEqual(self.resolve(), 4242)
        self.assert_no_claude_sweep()

    def test_other_affirmative_nonclaude_runtime_uses_scoped_pane(self):
        for runtime in ["gemini", "CODEX"]:
            with self.subTest(runtime=runtime):
                self.runtime = runtime
                self.assertEqual(self.resolve(), 4242)
                self.assert_no_claude_sweep()

    def test_writer_publishes_actual_scoped_core_pid(self):
        heartbeat.write_beat()
        value = json.loads((self.root / "fixture.alive").read_text())
        self.assertEqual(value["pid"], 4242)
        self.assertEqual(value["heartbeat_pid"], os.getpid())
        self.assertEqual(value["socket"], self.socket)
        self.assertEqual(value["session"], self.session)
        self.assertEqual(value["schema_version"], 4)
        self.assert_no_claude_sweep()

    def test_nonclaude_absent_exact_session_does_not_find_watcher_or_foreign(self):
        self.present = False
        self.assertIsNone(self.resolve())
        self.assert_no_claude_sweep()

    def test_nonclaude_empty_or_malformed_pane_list_is_absent(self):
        for panes in ["", "not-a-pid\n"]:
            with self.subTest(panes=panes):
                self.panes = panes
                self.assertIsNone(self.resolve())
                self.assert_no_claude_sweep()

    def test_nonclaude_failed_or_unavailable_pane_query_is_absent(self):
        for result in [1, None]:
            with self.subTest(result=result):
                self.pane_result = result
                self.assertIsNone(self.resolve())
                self.assert_no_claude_sweep()

    def test_claude_exact_pane_identity_still_wins(self):
        self.runtime = "claude"
        self.pane_argv = "claude --name fixture-core"
        self.assertEqual(self.resolve(), 4242)
        self.assertNotIn(("pgrep", "-x", "claude"), self.calls)

    def test_claude_wrapped_core_keeps_existing_process_fallback(self):
        self.runtime = "claude"
        self.pane_argv = "sh"
        self.assertEqual(self.resolve(), 9191)
        self.assertIn(("pgrep", "-x", "claude"), self.calls)

    def test_claude_dead_core_never_falls_back_to_shell_pane(self):
        self.runtime = "claude"
        self.pane_argv = "sh"
        self.foreign_present = False
        self.assertIsNone(self.resolve())

    def test_unknown_and_empty_runtime_keep_existing_claude_fallback(self):
        for runtime in [None, ""]:
            with self.subTest(runtime=runtime):
                self.runtime = runtime
                self.assertEqual(self.resolve(), 9191)
                self.assertIn(("pgrep", "-x", "claude"), self.calls)

    def test_unknown_runtime_becoming_claude_during_sweep_refuses_shell(self):
        self.runtime = None
        self.runtime_after_sweep = "claude"
        self.pane_argv = "sh"
        self.foreign_present = False
        self.assertIsNone(self.resolve())
        self.assertIn(("pgrep", "-x", "claude"), self.calls)

    def test_unknown_runtime_without_claude_keeps_scoped_pane_fallback(self):
        self.runtime = None
        self.foreign_present = False
        self.assertEqual(self.resolve(), 4242)


if __name__ == "__main__":
    unittest.main(verbosity=2)
