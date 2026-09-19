#!/usr/bin/env python3
"""The Claude core's managed task notifier pages when it is missing or dead.

Mirrors the Codex probe: a bare watcher tree can be green while the standby
notifier session is absent, so the generic watcher check cannot see this.
Run: python3 tests/health-check-claude-task-notifier.test.py
"""
from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

SUPERVISOR = REPO / "src" / "agent" / "codex" / "cli" / "task-notifier-supervisor.sh"
CLAUDE_NOTIFIER = REPO / "src" / "agent" / "claude" / "cli" / "task-notifier.sh"
CODEX_NOTIFIER = REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh"


class FakeTmux:
    def __init__(self, *, core_exists: bool = True, panes=None,
                 notifier_script: "str | None" = None) -> None:
        self.core_exists = core_exists
        self.panes = panes
        # What `show-environment` reports for SUTANDO_NOTIFIER_SCRIPT (None = unset).
        self.notifier_script = notifier_script
        self.calls = []

    @staticmethod
    def _result(args, returncode, stdout=""):
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    def __call__(self, socket, *args):
        self.calls.append((socket, args))
        if args[:3] == ("has-session", "-t", "=sutando-core"):
            return self._result(args, 0 if self.core_exists else 1)
        if len(args) >= 3 and args[:2] == ("has-session", "-t"):
            exists = args[2].endswith("-watcher") and self.panes is not None
            return self._result(args, 0 if exists else 1)
        if args and args[0] == "list-panes":
            if self.panes is None:
                return self._result(args, 1)
            return self._result(args, 0, "".join(f"{d}\t{c}\n" for d, c in self.panes))
        if args and args[0] == "show-environment":
            if self.notifier_script is None:
                return self._result(args, 1, "-SUTANDO_NOTIFIER_SCRIPT\n")
            return self._result(args, 0, f"SUTANDO_NOTIFIER_SCRIPT={self.notifier_script}\n")
        return self._result(args, 1)


class ClaudeTaskNotifierHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.state = self.workspace / "state"
        self.state.mkdir(parents=True)
        self.patches = [
            mock.patch.object(hc, "WORKSPACE_DIR", self.workspace),
            mock.patch.object(hc, "_host_label", return_value="local-host"),
            mock.patch.object(hc, "resolve_core_runtime", return_value="claude"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def write_local_core(self, *, socket="/tmp/test-sutando.sock", session="sutando-core") -> None:
        cores = self.state / "cores"
        cores.mkdir(exist_ok=True)
        (cores / "local-host.alive").write_text(
            json.dumps({"socket": socket, "session": session, "last_beat_at": time.time()}))

    def test_runtime_not_claude_means_not_expected(self):
        with mock.patch.object(hc, "resolve_core_runtime", return_value="codex"):
            result = hc.check_claude_task_notifier()
        self.assertEqual(result["status"], "ok")
        self.assertIn("not expected", result["detail"])

    def test_no_fresh_local_heartbeat_means_not_expected(self):
        tmux = FakeTmux()
        with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
            result = hc.check_claude_task_notifier()
        self.assertEqual(result["status"], "ok")
        self.assertIn("not expected", result["detail"])
        self.assertEqual(tmux.calls, [])

    def test_heartbeat_without_live_core_session_warns(self):
        self.write_local_core()
        tmux = FakeTmux(core_exists=False)
        with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
            result = hc.check_claude_task_notifier()
        self.assertEqual(result["status"], "warn")
        self.assertIn("could not be verified", result["detail"])

    def test_missing_watcher_session_warns_even_with_a_live_core(self):
        self.write_local_core()
        tmux = FakeTmux(panes=None)
        with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
            result = hc.check_claude_task_notifier()
        self.assertEqual(result["status"], "warn")
        self.assertIn("missing", result["detail"])

    def test_dead_pane_warns(self):
        self.write_local_core()
        tmux = FakeTmux(panes=[("1", f"bash {shlex.quote(str(SUPERVISOR))}")])
        with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
            result = hc.check_claude_task_notifier()
        self.assertEqual(result["status"], "warn")
        self.assertIn("dead pane", result["detail"])

    def test_unexpected_command_warns(self):
        self.write_local_core()
        tmux = FakeTmux(panes=[("0", "bash /elsewhere/other.sh")])
        with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
            result = hc.check_claude_task_notifier()
        self.assertEqual(result["status"], "warn")
        self.assertIn("unexpected command", result["detail"])

    def test_supervisor_on_its_default_notifier_warns(self):
        # A bare supervisor runs the Codex notifier; the pane command alone looks right.
        self.write_local_core()
        tmux = FakeTmux(panes=[("0", f"bash {shlex.quote(str(SUPERVISOR))}")], notifier_script=None)
        with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
            result = hc.check_claude_task_notifier()
        self.assertEqual(result["status"], "warn")
        self.assertIn("supervisor default", result["detail"])

    def test_supervisor_on_the_codex_notifier_warns(self):
        self.write_local_core()
        tmux = FakeTmux(panes=[("0", f"bash {shlex.quote(str(SUPERVISOR))}")],
                        notifier_script=str(CODEX_NOTIFIER))
        with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
            result = hc.check_claude_task_notifier()
        self.assertEqual(result["status"], "warn")
        self.assertIn("codex/cli/task-notifier.sh", result["detail"])

    def test_healthy_supervisor_pane_is_ok(self):
        self.write_local_core()
        tmux = FakeTmux(panes=[("0", f"bash {shlex.quote(str(SUPERVISOR))}")],
                        notifier_script=str(CLAUDE_NOTIFIER))
        with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
            result = hc.check_claude_task_notifier()
        self.assertEqual(result["status"], "ok", result)
        self.assertIn("sutando-core-watcher", result["detail"])

    def test_a_runtime_resolver_error_reads_as_not_selected(self):
        with mock.patch.object(hc, "resolve_core_runtime", side_effect=RuntimeError("no config")):
            self.assertFalse(hc._claude_runtime_selected())

    def test_target_reads_the_fresh_record_when_none_is_passed(self):
        with mock.patch.object(hc, "_fresh_local_core_record", return_value=None):
            self.assertIsNone(hc._local_claude_notifier_target())

    def test_a_record_missing_its_socket_or_session_yields_no_target(self):
        tmux = FakeTmux()
        with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
            self.assertIsNone(hc._local_claude_notifier_target({"session": "sutando-core"}))
            self.assertIsNone(hc._local_claude_notifier_target({"socket": "/tmp/t.sock"}))
            self.assertIsNone(hc._local_claude_notifier_target({"socket": "", "session": ""}))
        self.assertEqual(tmux.calls, [], "no tmux call without a complete record")

    def test_probe_is_registered_in_the_check_list(self):
        source = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_claude_task_notifier())", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
