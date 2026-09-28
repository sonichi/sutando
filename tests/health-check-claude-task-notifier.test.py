#!/usr/bin/env python3
"""The Claude core's managed task notifier pages when it is missing or dead.

Mirrors the Codex probe: a bare watcher tree can be green while the standby
notifier session is absent, so the generic watcher check cannot see this.
Run: python3 tests/health-check-claude-task-notifier.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
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
                 notifier_script: "str | None" = None, core_runtime: "str | None" = "claude") -> None:
        self.core_exists = core_exists
        self.panes = panes
        # What `show-environment` reports for SUTANDO_NOTIFIER_SCRIPT (None = unset).
        self.notifier_script = notifier_script
        # What the core session records as SUTANDO_CORE_RUNTIME (None = unset).
        self.core_runtime = core_runtime
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
        if args and args[0] == "show-environment" and args[-1] == "SUTANDO_CORE_RUNTIME":
            if self.core_runtime is None:
                return self._result(args, 1, "-SUTANDO_CORE_RUNTIME\n")
            return self._result(args, 0, f"SUTANDO_CORE_RUNTIME={self.core_runtime}\n")
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

    def write_local_core(self, *, socket="/tmp/test-sutando.sock", session="sutando-core", pid=None) -> None:
        cores = self.state / "cores"
        cores.mkdir(exist_ok=True)
        (cores / "local-host.alive").write_text(
            json.dumps({"socket": socket, "session": session, "last_beat_at": time.time(),
                        "pid": os.getpid() if pid is None else pid}))

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

    def test_fix_repairs_a_missing_watcher_through_the_launcher_and_preserves_the_core(self):
        """P1-23: the standby notifier went missing and nothing under --fix brought it
        back. The repair runs the canonical launcher (no --restart) for the live core's
        socket/session and reports by re-probing, never by trusting the launcher."""
        self.write_local_core(socket="/tmp/custom.sock", session="sutando-core")
        tmux = FakeTmux(panes=None)  # the watcher session is gone
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"], seen["env"] = argv, kw.get("env", {})
            tmux.panes = [("0", f"bash {shlex.quote(str(SUPERVISOR))}")]  # the launcher recreated it
            tmux.notifier_script = str(CLAUDE_NOTIFIER)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(hc, "_run_tmux", side_effect=tmux), \
                mock.patch.object(hc, "_resolve_launch_env", return_value={"PATH": "/usr/bin"}), \
                mock.patch.object(hc.subprocess, "run", fake_run):
            self.assertEqual(hc.fix_claude_task_notifier(), "repaired managed notifier; live core session preserved")
        self.assertTrue(str(seen["argv"][1]).endswith("src/agent/start-cli.sh"))
        self.assertNotIn("--restart", seen["argv"])
        self.assertEqual(seen["env"]["SUTANDO_CORE_RUNTIME"], "claude")
        self.assertEqual(seen["env"]["SUTANDO_TMUX_SOCKET"], "/tmp/custom.sock")
        self.assertEqual(seen["env"]["SUTANDO_TMUX_SESSION"], "sutando-core")

    def test_fix_reports_what_it_could_not_repair(self):
        with mock.patch.object(hc, "resolve_core_runtime", return_value="codex"), \
                mock.patch.object(hc.subprocess, "run") as run:
            self.assertIn("not selected", hc.fix_claude_task_notifier())
            run.assert_not_called()
        with mock.patch.object(hc.subprocess, "run") as run:
            self.assertIn("no fresh local Claude core heartbeat", hc.fix_claude_task_notifier())
            run.assert_not_called()
        self.write_local_core()
        healthy = FakeTmux(panes=[("0", f"bash {shlex.quote(str(SUPERVISOR))}")], notifier_script=str(CLAUDE_NOTIFIER))
        with mock.patch.object(hc, "_run_tmux", side_effect=healthy), mock.patch.object(hc.subprocess, "run") as run:
            self.assertEqual(hc.fix_claude_task_notifier(), "already healthy")
            run.assert_not_called()
        gone = FakeTmux(panes=None)
        with mock.patch.object(hc, "_run_tmux", side_effect=gone), \
                mock.patch.object(hc, "_resolve_launch_env", return_value={}), \
                mock.patch.object(hc.subprocess, "run", return_value=subprocess.CompletedProcess([], 3, "", "boom\n")):
            self.assertEqual(hc.fix_claude_task_notifier(), "not repaired — launcher exited 3: boom")
        with mock.patch.object(hc, "_run_tmux", side_effect=gone), \
                mock.patch.object(hc, "_resolve_launch_env", return_value={}), \
                mock.patch.object(hc.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertIn("not repaired — ", hc.fix_claude_task_notifier(), "a launcher that exits 0 but leaves no watcher is not a repair")

    def test_fix_refuses_a_session_that_records_another_runtime(self):
        """Review of #4872 (Yixuan, Rui): the shared launcher injects --restart when the
        session records a different runtime, so a Codex core in the session would be
        restarted by a repair that claims to preserve it. Refused before the launcher."""
        self.write_local_core()
        for recorded in ("codex", None):
            with self.subTest(recorded=recorded):
                tmux = FakeTmux(panes=None, core_runtime=recorded)
                with mock.patch.object(hc, "_run_tmux", side_effect=tmux), \
                        mock.patch.object(hc.subprocess, "run") as run:
                    verdict = hc.fix_claude_task_notifier()
                self.assertIn("records a different runtime", verdict)
                self.assertIn("codex" if recorded else "unreadable", verdict)
                run.assert_not_called()

    def test_process_alive_answers_for_every_kill_outcome(self):
        self.assertTrue(hc._process_alive(os.getpid()))
        self.assertFalse(hc._process_alive(0))
        self.assertFalse(hc._process_alive(-1))
        for exc, expected in ((ProcessLookupError(), False), (PermissionError(), True), (OSError("odd"), False)):
            with mock.patch.object(hc.os, "kill", side_effect=exc):
                self.assertIs(hc._process_alive(4242), expected, type(exc).__name__)

    def test_fix_refuses_when_the_heartbeat_core_process_is_gone(self):
        """Rui: in the 60-90 s after the core dies its heartbeat is still fresh; the
        launcher would spawn a NEW core while the repair reported a preserved one."""
        self.write_local_core(pid=2_000_000_000)  # no such process
        with mock.patch.object(hc, "_run_tmux", side_effect=FakeTmux(panes=None)), \
                mock.patch.object(hc.subprocess, "run") as run:
            verdict = hc.fix_claude_task_notifier()
        self.assertIn("core process (pid 2000000000) is not running", verdict)
        run.assert_not_called()
        self.write_local_core(pid="not-a-pid")
        with mock.patch.object(hc, "_run_tmux", side_effect=FakeTmux(panes=None)), \
                mock.patch.object(hc.subprocess, "run") as run:
            self.assertIn("is not running", hc.fix_claude_task_notifier())
            run.assert_not_called()

    def test_fix_reports_every_way_the_launcher_path_can_fail(self):
        self.write_local_core()
        # The core session cannot be verified: nothing to repair against.
        with mock.patch.object(hc, "_run_tmux", side_effect=FakeTmux(core_exists=False)), \
                mock.patch.object(hc.subprocess, "run") as run:
            self.assertIn("could not be verified", hc.fix_claude_task_notifier())
            run.assert_not_called()
        gone = FakeTmux(panes=None)
        # The launcher is missing from this checkout.
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(hc, "REPO_DIR", Path(td)), \
                mock.patch.object(hc, "_run_tmux", side_effect=gone), \
                mock.patch.object(hc.subprocess, "run") as run:
            self.assertIn("launcher is missing", hc.fix_claude_task_notifier())
            run.assert_not_called()
        # The launcher cannot be run at all.
        with mock.patch.object(hc, "_run_tmux", side_effect=gone), \
                mock.patch.object(hc, "_resolve_launch_env", return_value={}), \
                mock.patch.object(hc.subprocess, "run", side_effect=subprocess.TimeoutExpired("start-cli.sh", 120)):
            self.assertEqual(hc.fix_claude_task_notifier(), "not repaired — launcher failed (TimeoutExpired)")
        # The local core changed under the repair: the launcher's result is not ours to claim.
        calls = []

        def drifting_target(hb=None):
            # The check itself resolves the target once more; only the post-launch read drifts.
            calls.append(1)
            sock = "/tmp/test-sutando.sock" if len(calls) < 3 else "/tmp/other.sock"
            return {"socket": sock, "session": "sutando-core"}
        with mock.patch.object(hc, "_run_tmux", side_effect=gone), \
                mock.patch.object(hc, "_local_claude_notifier_target", drifting_target), \
                mock.patch.object(hc, "_resolve_launch_env", return_value={}), \
                mock.patch.object(hc.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(hc.fix_claude_task_notifier(), "not repaired — local Claude core changed during repair")

    def test_the_fix_loop_dispatches_the_claude_repair(self):
        import io
        from contextlib import redirect_stdout
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("elif codex_notifier is None and claude_notifier is None:", src)
        checks = [{"name": "claude-task-notifier", "status": "warn",
                   "detail": "managed tmux session 'sutando-core-watcher' is missing"}]
        out = io.StringIO()
        with mock.patch.object(hc.sys, "argv", ["health-check.py", "--fix"]), \
                mock.patch.object(hc, "run_all_checks", return_value=checks), \
                mock.patch.object(hc, "fix_down_bridges", return_value=[]), \
                mock.patch.object(hc, "fix_claude_task_notifier", return_value="repaired managed notifier; live core session preserved") as fix, \
                mock.patch("time.sleep", lambda *_: None):
            try:
                with redirect_stdout(out):
                    hc.main()
            except SystemExit:
                pass
        self.assertIn("claude-task-notifier: repaired managed notifier; live core session preserved", out.getvalue())
        fix.assert_called_once()

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
