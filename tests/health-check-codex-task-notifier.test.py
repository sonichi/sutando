#!/usr/bin/env python3
"""Regression coverage for the managed Codex task-notifier health check.

Run: python3 tests/health-check-codex-task-notifier.test.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "health_check", REPO / "src" / "health-check.py"
)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class FakeTmux:
    def __init__(
        self,
        *,
        core_runtime: str = "codex",
        panes: list[tuple[str, str]] | None = None,
        core_exists: bool = True,
    ) -> None:
        self.core_runtime = core_runtime
        self.panes = panes
        self.core_exists = core_exists
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    @staticmethod
    def _result(args: tuple[str, ...], returncode: int, stdout: str = ""):
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    def __call__(self, socket: str, *args: str):
        self.calls.append((socket, args))
        if args[:3] == ("has-session", "-t", "=sutando-core"):
            return self._result(args, 0 if self.core_exists else 1)
        if len(args) >= 3 and args[:2] == ("has-session", "-t"):
            target = args[2]
            exists = target.endswith("-watcher") and self.panes is not None
            return self._result(args, 0 if exists else 1)
        if args[:3] == ("show-environment", "-t", "=sutando-core"):
            return self._result(
                args, 0, f"SUTANDO_CORE_RUNTIME={self.core_runtime}\n"
            )
        if args and args[0] == "list-panes":
            if self.panes is None:
                return self._result(args, 1)
            rows = "".join(f"{dead}\t{command}\n" for dead, command in self.panes)
            return self._result(args, 0, rows)
        return self._result(args, 1)


class CodexTaskNotifierHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.state = self.workspace / "state"
        self.state.mkdir(parents=True)
        self.patches = [
            mock.patch.object(hc, "WORKSPACE_DIR", self.workspace),
            mock.patch.object(hc, "_host_label", return_value="local-host"),
            mock.patch.object(hc, "resolve_core_runtime", return_value="codex"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def write_local_core(
        self,
        *,
        socket: str = "/tmp/test-sutando.sock",
        session: str = "sutando-core",
        runtime: str = "codex",
    ) -> None:
        cores = self.state / "cores"
        cores.mkdir(exist_ok=True)
        (cores / "local-host.alive").write_text(
            json.dumps({"socket": socket, "last_beat_at": time.time()})
        )
        (self.state / "core-runtime.json").write_text(
            json.dumps({"runtime": runtime, "session": session})
        )

    def direct_notifier(self) -> str:
        return f"bash {REPO / 'src/agent/codex/cli/task-notifier.sh'}"

    def expected_notifier(self) -> str:
        # Quote like tmux does. An unquoted path splits on its spaces, so this
        # suite failed on any checkout under e.g. ~/Library/Application Support.
        return f"bash {shlex.quote(str(hc._expected_codex_notifier_entrypoint()))}"

    def test_bare_watcher_can_be_green_while_managed_notifier_is_missing(self):
        self.write_local_core()
        (self.state / "watch-tasks-stream.pid").write_text("4242")
        tmux = FakeTmux(panes=None)
        with (
            mock.patch.object(
                hc, "_proc_argv", return_value="bash src/watch-tasks-stream.sh"
            ),
            mock.patch.object(
                hc, "_watcher_trees", return_value={"4200": {"4200", "4242"}}
            ),
            # check_task_watcher() returns early when ps is unavailable, so
            # without this the generic assertion measures the host, not the code.
            mock.patch.object(
                hc, "_ps_snapshot", return_value="PID TT STAT TIME COMMAND\n"
            ),
            mock.patch.object(hc, "_run_tmux", side_effect=tmux),
        ):
            generic = hc.check_task_watcher()
            managed = hc.check_codex_task_notifier()
        self.assertEqual(generic["status"], "ok")
        self.assertEqual(managed["status"], "warn")
        self.assertIn("missing", managed["detail"])

    def test_no_fresh_local_heartbeat_means_notifier_is_not_expected(self):
        cores = self.state / "cores"
        cores.mkdir()
        (cores / "remote-host.alive").write_text("{}")
        tmux = FakeTmux(panes=None)
        with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
            result = hc.check_codex_task_notifier()
        self.assertEqual(result["status"], "ok")
        self.assertIn("not expected", result["detail"])
        self.assertEqual(tmux.calls, [])

    def test_stale_or_malformed_local_heartbeat_is_not_treated_as_live(self):
        cores = self.state / "cores"
        cores.mkdir()
        alive = cores / "local-host.alive"
        cases = ("missing", "stale", "malformed", "not-an-object")
        for label in cases:
            with self.subTest(label=label):
                alive.unlink(missing_ok=True)
                if label == "stale":
                    alive.write_text("{}")
                    old = time.time() - 91
                    os.utime(alive, (old, old))
                elif label == "malformed":
                    alive.write_text("{")
                elif label == "not-an-object":
                    alive.write_text("[]")
                self.assertIsNone(hc._fresh_local_core_record())

    def test_unusable_runtime_metadata_warns_without_touching_tmux(self):
        self.write_local_core()
        heartbeat = self.state / "cores" / "local-host.alive"
        runtime = self.state / "core-runtime.json"
        cases = ("missing-socket", "malformed-state", "wrong-runtime", "missing-session")
        for label in cases:
            with self.subTest(label=label):
                self.write_local_core()
                if label == "missing-socket":
                    heartbeat.write_text("{}")
                elif label == "malformed-state":
                    runtime.write_text("{")
                elif label == "wrong-runtime":
                    runtime.write_text(json.dumps({"runtime": "claude"}))
                elif label == "missing-session":
                    runtime.write_text(json.dumps({"runtime": "codex"}))
                tmux = FakeTmux(panes=None)
                with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
                    result = hc.check_codex_task_notifier()
                self.assertEqual(result["status"], "warn")
                self.assertIn("metadata", result["detail"])
                self.assertEqual(tmux.calls, [])

    def test_non_codex_core_does_not_require_notifier(self):
        # The Codex launcher owns core-runtime.json, so this file can remain
        # stale after a config switch to Claude.
        self.write_local_core(runtime="codex")
        tmux = FakeTmux(core_runtime="claude", panes=None)
        with (
            mock.patch.object(hc, "resolve_core_runtime", return_value="claude"),
            mock.patch.object(hc, "_run_tmux", side_effect=tmux),
            mock.patch.object(hc.subprocess, "run") as run,
        ):
            result = hc.check_codex_task_notifier()
            repaired = hc.fix_codex_task_notifier()
        self.assertEqual(result["status"], "ok")
        self.assertIn("not expected", result["detail"])
        self.assertIn("not selected", repaired)
        self.assertEqual(tmux.calls, [])
        run.assert_not_called()

    def test_invalid_runtime_config_disables_any_automatic_action(self):
        self.write_local_core()
        with (
            mock.patch.object(
                hc, "resolve_core_runtime", side_effect=ValueError("invalid")
            ),
            mock.patch.object(hc.subprocess, "run") as run,
        ):
            check = hc.check_codex_task_notifier()
            repaired = hc.fix_codex_task_notifier()
        self.assertEqual(check["status"], "ok")
        self.assertIn("not selected", repaired)
        run.assert_not_called()

    def test_runtime_authored_socket_and_session_are_honored(self):
        self.write_local_core(socket="/tmp/custom.sock", session="kewei-core")
        tmux = FakeTmux(panes=[("0", self.expected_notifier())])

        def custom_tmux(socket: str, *args: str):
            tmux.calls.append((socket, args))
            if args[:3] == ("has-session", "-t", "=kewei-core"):
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ("show-environment", "-t", "=kewei-core"):
                return subprocess.CompletedProcess(
                    args, 0, "SUTANDO_CORE_RUNTIME=codex\n", ""
                )
            if args[:3] == ("has-session", "-t", "=kewei-core-watcher"):
                return subprocess.CompletedProcess(args, 0, "", "")
            if args and args[0] == "list-panes":
                return subprocess.CompletedProcess(
                    args, 0, f"0\t{self.expected_notifier()}\n", ""
                )
            return subprocess.CompletedProcess(args, 1, "", "")

        with mock.patch.object(hc, "_run_tmux", side_effect=custom_tmux):
            result = hc.check_codex_task_notifier()
        self.assertEqual(result["status"], "ok", result["detail"])
        self.assertTrue(all(socket == "/tmp/custom.sock" for socket, _ in tmux.calls))
        self.assertTrue(
            any("=kewei-core-watcher" in args for _, args in tmux.calls)
        )

    def test_current_direct_notifier_is_accepted(self):
        self.write_local_core()
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            direct = repo / "src/agent/codex/cli/task-notifier.sh"
            direct.parent.mkdir(parents=True)
            direct.write_text("#!/bin/bash\n")
            tmux = FakeTmux(panes=[("0", f"bash {direct}")])
            with (
                mock.patch.object(hc, "REPO_DIR", repo),
                mock.patch.object(hc, "_run_tmux", side_effect=tmux),
            ):
                result = hc.check_codex_task_notifier()
        self.assertEqual(result["status"], "ok", result["detail"])

    def test_supervisor_is_accepted_when_present(self):
        self.write_local_core()
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            supervisor = repo / "src/agent/codex/cli/task-notifier-supervisor.sh"
            supervisor.parent.mkdir(parents=True)
            supervisor.write_text("#!/bin/bash\n")
            tmux = FakeTmux(panes=[("0", f"bash {supervisor}")])
            with (
                mock.patch.object(hc, "REPO_DIR", repo),
                mock.patch.object(hc, "_run_tmux", side_effect=tmux),
            ):
                result = hc.check_codex_task_notifier()
        self.assertEqual(result["status"], "ok", result["detail"])

    def test_notifier_command_handles_tmux_shell_quoting(self):
        with tempfile.TemporaryDirectory(prefix="sutando notifier ") as repo_td:
            expected = (
                Path(repo_td) / "src/agent/codex/cli/task-notifier.sh"
            )
            expected.parent.mkdir(parents=True)
            expected.write_text("#!/bin/bash\n")
            for command in (
                f"bash {shlex.quote(str(expected))}",
                f'/bin/bash "{expected}"',
                f"bash -- {shlex.quote(str(expected))}",
                shlex.quote(str(expected)),
            ):
                with self.subTest(command=command):
                    self.assertTrue(hc._command_runs_script(command, expected))

    def test_expected_notifier_path_only_as_text_is_rejected(self):
        expected = REPO / "src/agent/codex/cli/task-notifier.sh"
        cases = {
            "argument": f"bash /tmp/notifier-wrapper.sh --label {expected}",
            "suffix": f"bash {expected}.backup",
            "comment": f"bash /tmp/notifier-wrapper.sh '# {expected}'",
            "shell-command": f"bash -lc 'exec {expected}'",
            "malformed-quoting": f"bash '{expected}",
        }
        for label, command in cases.items():
            with self.subTest(label=label):
                self.assertFalse(hc._command_runs_script(command, expected))

    def test_probe_rejects_notifier_path_only_as_wrapper_argument(self):
        self.write_local_core()
        expected = hc._expected_codex_notifier_entrypoint()
        tmux = FakeTmux(
            panes=[
                (
                    "0",
                    f"bash /tmp/notifier-wrapper.sh --label {expected}",
                )
            ]
        )
        with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
            result = hc.check_codex_task_notifier()
        self.assertEqual(result["status"], "warn")
        self.assertIn("unexpected command", result["detail"])

    def test_dead_wrong_and_duplicate_panes_warn(self):
        self.write_local_core()
        cases = {
            "dead": [("1", self.expected_notifier())],
            "wrong": [("0", "bash /tmp/not-the-notifier.sh")],
            "duplicate": [
                ("0", self.expected_notifier()),
                ("0", self.expected_notifier()),
            ],
        }
        for label, panes in cases.items():
            with self.subTest(label=label):
                tmux = FakeTmux(panes=panes)
                with mock.patch.object(hc, "_run_tmux", side_effect=tmux):
                    result = hc.check_codex_task_notifier()
                self.assertEqual(result["status"], "warn")

    def test_probe_and_tmux_errors_warn_instead_of_raising(self):
        target = {"socket": "/tmp/test-sutando.sock", "session": "sutando-core"}

        def cannot_list(socket: str, *args: str):
            return subprocess.CompletedProcess(
                args, 1 if args[0] == "list-panes" else 0, "", ""
            )

        with mock.patch.object(hc, "_run_tmux", side_effect=cannot_list):
            result = hc._probe_codex_task_notifier(target)
        self.assertEqual(result["status"], "warn")
        self.assertIn("cannot inspect", result["detail"])

        def malformed_list(socket: str, *args: str):
            stdout = "malformed-row\n" if args[0] == "list-panes" else ""
            return subprocess.CompletedProcess(args, 0, stdout, "")

        with mock.patch.object(hc, "_run_tmux", side_effect=malformed_list):
            result = hc._probe_codex_task_notifier(target)
        self.assertEqual(result["status"], "warn")
        self.assertIn("expected exactly 1", result["detail"])

        with mock.patch.object(hc.subprocess, "run", side_effect=OSError("tmux")):
            self.assertIsNone(hc._run_tmux(target["socket"], "list-sessions"))

    def test_unverifiable_core_warns_and_is_never_repaired(self):
        self.write_local_core()
        for label, tmux in (
            ("missing", FakeTmux(core_exists=False, panes=None)),
            ("wrong-runtime", FakeTmux(core_runtime="claude", panes=None)),
        ):
            with self.subTest(label=label):
                with (
                    mock.patch.object(hc, "_run_tmux", side_effect=tmux),
                    mock.patch.object(hc.subprocess, "run") as run,
                ):
                    check = hc.check_codex_task_notifier()
                    repaired = hc.fix_codex_task_notifier()
                self.assertEqual(check["status"], "warn")
                self.assertIn("not repaired", repaired)
                run.assert_not_called()

    def test_fix_recreates_missing_session_without_restarting_core(self):
        self.write_local_core()
        tmux = FakeTmux(panes=None)
        launcher_calls = []

        def run_launcher(args, **kwargs):
            launcher_calls.append((args, kwargs))
            tmux.panes = [("0", self.expected_notifier())]
            return subprocess.CompletedProcess(args, 0, "sutando-core already running", "")

        with (
            mock.patch.object(hc, "_run_tmux", side_effect=tmux),
            mock.patch.object(hc.subprocess, "run", side_effect=run_launcher),
        ):
            result = hc.fix_codex_task_notifier()

        self.assertEqual(result, "repaired managed notifier; live core session preserved")
        self.assertEqual(len(launcher_calls), 1)
        args, kwargs = launcher_calls[0]
        self.assertEqual(
            args, ["/bin/bash", str(REPO / "src/agent/start-cli.sh")]
        )
        self.assertNotIn("--restart", args)
        self.assertEqual(kwargs["env"]["SUTANDO_TMUX_SOCKET"], "/tmp/test-sutando.sock")
        self.assertEqual(kwargs["env"]["SUTANDO_TMUX_SESSION"], "sutando-core")
        self.assertEqual(kwargs["env"]["SUTANDO_CORE_RUNTIME"], "codex")
        tmux_commands = [args[0] for _, args in tmux.calls]
        self.assertNotIn("kill-session", tmux_commands)
        self.assertNotIn("new-session", tmux_commands)

    def test_fix_failure_paths_fail_closed(self):
        self.write_local_core()
        target = {"socket": "/tmp/test-sutando.sock", "session": "sutando-core"}
        warning = {
            "name": "codex-task-notifier",
            "status": "warn",
            "detail": "managed tmux session is missing",
        }
        healthy = {
            "name": "codex-task-notifier",
            "status": "ok",
            "detail": "healthy",
        }

        with mock.patch.object(hc, "_local_codex_core_target", return_value=None):
            self.assertIn("no verified", hc.fix_codex_task_notifier())

        with (
            mock.patch.object(hc, "_local_codex_core_target", return_value=target),
            mock.patch.object(
                hc, "_probe_codex_task_notifier", return_value=healthy
            ),
        ):
            self.assertEqual(hc.fix_codex_task_notifier(), "already healthy")

        with tempfile.TemporaryDirectory() as repo_td:
            with (
                mock.patch.object(hc, "REPO_DIR", Path(repo_td)),
                mock.patch.object(
                    hc, "_local_codex_core_target", return_value=target
                ),
                mock.patch.object(
                    hc, "_probe_codex_task_notifier", return_value=warning
                ),
            ):
                self.assertIn("launcher is missing", hc.fix_codex_task_notifier())

        with (
            mock.patch.object(hc, "_local_codex_core_target", return_value=target),
            mock.patch.object(
                hc, "_probe_codex_task_notifier", return_value=warning
            ),
            mock.patch.object(hc.subprocess, "run", side_effect=OSError("boom")),
        ):
            self.assertIn("launcher failed (OSError)", hc.fix_codex_task_notifier())

        failed = subprocess.CompletedProcess([], 23, "", "launcher exploded")
        with (
            mock.patch.object(hc, "_local_codex_core_target", return_value=target),
            mock.patch.object(
                hc, "_probe_codex_task_notifier", return_value=warning
            ),
            mock.patch.object(hc.subprocess, "run", return_value=failed),
        ):
            self.assertIn("launcher exited 23: launcher exploded", hc.fix_codex_task_notifier())

        launched = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(
                hc, "_local_codex_core_target", side_effect=[target, {**target, "session": "other"}]
            ),
            mock.patch.object(
                hc, "_probe_codex_task_notifier", return_value=warning
            ),
            mock.patch.object(hc.subprocess, "run", return_value=launched),
        ):
            self.assertIn("core changed", hc.fix_codex_task_notifier())

        with (
            mock.patch.object(
                hc, "_local_codex_core_target", side_effect=[target, target]
            ),
            mock.patch.object(
                hc, "_probe_codex_task_notifier", side_effect=[warning, warning]
            ),
            mock.patch.object(hc.subprocess, "run", return_value=launched),
        ):
            self.assertIn("managed tmux session is missing", hc.fix_codex_task_notifier())

    def test_check_and_warn_only_fix_paths_are_registered(self):
        source = (REPO / "src/health-check.py").read_text()
        self.assertIn("checks.append(check_codex_task_notifier())", source)
        checks = [
            {
                "name": "codex-task-notifier",
                "status": "warn",
                "detail": "missing",
            }
        ]
        with (
            mock.patch.object(hc, "run_all_checks", return_value=checks),
            mock.patch.object(
                hc,
                "fix_codex_task_notifier",
                return_value="repaired managed notifier",
            ) as fix,
            mock.patch.object(hc, "fix_down_bridges", return_value=[]),
            mock.patch.object(
                sys, "argv", ["health-check.py", "--fix", "--quiet"]
            ),
            redirect_stdout(StringIO()) as output,
            self.assertRaises(SystemExit) as exited,
        ):
            hc.main()
        self.assertEqual(exited.exception.code, 0)
        fix.assert_called_once_with()
        self.assertIn(
            "codex-task-notifier: repaired managed notifier", output.getvalue()
        )


if __name__ == "__main__":
    unittest.main()
