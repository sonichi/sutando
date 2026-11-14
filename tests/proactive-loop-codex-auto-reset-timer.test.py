#!/usr/bin/env python3
"""Exercise the launchd contract without calling the host's launchctl."""

from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/proactive-loop/scripts/codex-auto-reset-timer.py"
SPEC = importlib.util.spec_from_file_location("codex_auto_reset_timer", SCRIPT)
assert SPEC and SPEC.loader
t = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t)


class FakeLaunchctl:
    def __init__(self):
        self.loaded = set()
        self.calls = []
        self.fail_bootstrap = False

    def __call__(self, argv):
        self.calls.append(argv)
        verb = argv[1]
        target = argv[-1]
        if verb == "print":
            return subprocess.CompletedProcess(argv, 0 if target in self.loaded else 113, "", "")
        if verb == "bootout":
            self.loaded.discard(target)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "bootstrap":
            if self.fail_bootstrap:
                self.fail_bootstrap = False
                return subprocess.CompletedProcess(argv, 5, "", "bootstrap failed")
            with open(target, "rb") as stream:
                label = plistlib.load(stream)["Label"]
            self.loaded.add(f"gui/{os.getuid()}/{label}")
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    def count(self, verb):
        return sum(argv[1] == verb for argv in self.calls)


class TimerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / "workspace"
        self.home = self.root / "codex-home"
        self.agents = self.root / "LaunchAgents"
        self.codex = self.root / "codex"
        self.codex.write_text("#!/bin/sh\nexit 0\n")
        self.codex.chmod(0o700)
        self.launchctl = FakeLaunchctl()

    def ensure(self, workspace=None, home=None, **kwargs):
        return t.ensure(workspace or self.workspace, home or self.home,
                        codex_bin=self.codex, launch_agents=self.agents,
                        runner=self.launchctl, sleep=lambda _: None, **kwargs)

    def read_job(self, home=None):
        with open(t.plist_path(home or self.home, self.agents), "rb") as stream:
            return plistlib.load(stream)

    def test_job_runs_one_tick_per_five_minutes_with_stable_executables(self):
        with mock.patch.dict(os.environ, {"SUTANDO_CODEX_AUTO_RESET_ENABLED": "0"}):
            result = self.ensure()
        job = self.read_job()
        self.assertTrue(result["loaded"])
        self.assertEqual(job["Label"], t.label_for(self.home))
        self.assertEqual(job["StartInterval"], 300)
        self.assertTrue(job["RunAtLoad"])
        self.assertEqual(job["ProgramArguments"][1:], [
            str(REPO / "skills/proactive-loop/scripts/codex-auto-reset.py"),
            "--workspace", str(self.workspace.resolve()),
            "--codex-home", str(self.home.resolve()),
            "--codex-bin", str(self.codex.absolute()), "--json"])
        self.assertTrue(Path(job["ProgramArguments"][0]).is_absolute())
        self.assertTrue((self.workspace / "logs").is_dir())
        self.assertEqual(job["StandardOutPath"],
                         str(self.workspace.resolve() / "logs/codex-auto-reset.log"))
        self.assertEqual(job["EnvironmentVariables"]["CODEX_HOME"], str(self.home.resolve()))
        self.assertEqual(job["EnvironmentVariables"]["SUTANDO_CODEX_AUTO_RESET_ENABLED"], "0")
        self.assertEqual(job["EnvironmentVariables"]["PATH"].split(os.pathsep)[0],
                         str(self.codex.parent.absolute()))

    def test_ensure_does_not_rebootstrap_unchanged_loaded_job(self):
        first = self.ensure(enabled_override="1")
        second = self.ensure(enabled_override="1")
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(self.launchctl.count("bootstrap"), 1)
        self.assertEqual(self.launchctl.count("bootout"), 0)

    def test_missing_override_preserves_prior_disabled_policy(self):
        self.ensure(enabled_override="0")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.ensure(workspace=self.root / "other-workspace")
        self.assertEqual(self.read_job()["EnvironmentVariables"][
            "SUTANDO_CODEX_AUTO_RESET_ENABLED"], "0")
        self.ensure(enabled_override="1")
        self.assertEqual(self.read_job()["EnvironmentVariables"][
            "SUTANDO_CODEX_AUTO_RESET_ENABLED"], "1")

    def test_symlinked_binaries_remain_upgradeable(self):
        link = self.root / "bin/codex"
        link.parent.mkdir()
        link.symlink_to(self.codex)
        python_link = self.root / "bin/python"
        python_link.symlink_to(sys.executable)
        job = t.render(self.workspace, self.home, codex_bin=link,
                       python=python_link)
        self.assertEqual(job["ProgramArguments"][-3:-1],
                         ["--codex-bin", str(link.absolute())])
        self.assertEqual(job["ProgramArguments"][0], str(python_link.absolute()))
        newer = self.root / "codex-new"
        newer.write_text("#!/bin/sh\nexit 0\n")
        newer.chmod(0o700)
        link.unlink()
        link.symlink_to(newer)
        self.assertEqual(t.render(self.workspace, self.home, codex_bin=link,
                                  python=python_link)["ProgramArguments"],
                         job["ProgramArguments"])

    def test_same_home_has_one_job_across_workspaces(self):
        other = self.root / "other-workspace"
        self.ensure()
        self.ensure(workspace=other)
        self.assertEqual(len(list(self.agents.glob("*.plist"))), 1)
        self.assertEqual(self.read_job()["ProgramArguments"][3], str(other.resolve()))
        self.assertEqual(self.launchctl.count("bootstrap"), 2)
        self.assertEqual(self.launchctl.count("bootout"), 1)

    def test_different_homes_have_independent_jobs(self):
        other = self.root / "other-codex-home"
        self.ensure()
        self.ensure(home=other)
        self.assertNotEqual(t.label_for(self.home), t.label_for(other))
        self.assertEqual(len(list(self.agents.glob("*.plist"))), 2)
        self.assertTrue(t.status(self.home, launch_agents=self.agents,
                                 runner=self.launchctl)["loaded"])
        self.assertTrue(t.status(other, launch_agents=self.agents,
                                 runner=self.launchctl)["loaded"])

    def test_failed_reinstall_restores_previous_plist_and_loaded_job(self):
        self.ensure(enabled_override="1")
        dest = t.plist_path(self.home, self.agents)
        old = dest.read_bytes()
        self.launchctl.fail_bootstrap = True
        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            self.ensure(enabled_override="0")
        self.assertEqual(dest.read_bytes(), old)
        self.assertTrue(t.status(self.home, launch_agents=self.agents,
                                 runner=self.launchctl)["loaded"])

    def test_failed_first_bootstrap_leaves_no_installed_looking_plist(self):
        self.launchctl.fail_bootstrap = True
        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            self.ensure()
        result = t.status(self.home, launch_agents=self.agents, runner=self.launchctl)
        self.assertFalse(result["installed"])
        self.assertFalse(result["loaded"])

    def test_uninstall_is_repeatable_and_specific_to_one_home(self):
        other = self.root / "other-home"
        self.ensure()
        self.ensure(home=other)
        result = t.uninstall(self.home, launch_agents=self.agents,
                             runner=self.launchctl, sleep=lambda _: None)
        self.assertTrue(result["removed"])
        self.assertFalse(result["loaded"])
        again = t.uninstall(self.home, launch_agents=self.agents,
                            runner=self.launchctl, sleep=lambda _: None)
        self.assertFalse(again["removed"])
        self.assertTrue(t.status(other, launch_agents=self.agents,
                                 runner=self.launchctl)["loaded"])

    def test_missing_codex_binary_fails_before_touching_launchd(self):
        with self.assertRaisesRegex(ValueError, "not runnable"):
            t.render(self.workspace, self.home, codex_bin=self.root / "missing")
        self.assertEqual(self.launchctl.calls, [])
        self.assertFalse(self.agents.exists())

    def test_cli_install_status_uninstall_with_launchd_transport(self):
        def launchctl(argv, runner=None):
            return self.launchctl(["launchctl", *argv])

        base = ["--codex-home", str(self.home), "--launch-agents", str(self.agents)]
        output = io.StringIO()
        with mock.patch.object(t, "_launchctl", side_effect=launchctl), redirect_stdout(output):
            self.assertEqual(t.main(["install", "--workspace", str(self.workspace),
                                     "--codex-bin", str(self.codex), *base]), 0)
            self.assertEqual(t.main(["status", *base]), 0)
            self.assertEqual(t.main(["uninstall", *base]), 0)
        self.assertIn("installed: True", output.getvalue())
        self.assertIn("removed: True", output.getvalue())
        self.assertFalse(t.plist_path(self.home, self.agents).exists())

    def test_cli_refuses_bad_install_without_launching(self):
        error = io.StringIO()
        with redirect_stderr(error):
            rc = t.main(["install", "--workspace", str(self.workspace),
                         "--codex-home", str(self.home),
                         "--codex-bin", str(self.root / "missing"),
                         "--launch-agents", str(self.agents)])
        self.assertEqual(rc, 1)
        self.assertIn("not runnable", error.getvalue())
        self.assertEqual(self.launchctl.calls, [])

    def test_unreadable_existing_timer_cannot_clear_a_disable(self):
        self.ensure(enabled_override="0")
        dest = t.plist_path(self.home, self.agents)
        job = self.read_job()
        job["EnvironmentVariables"] = []
        with open(dest, "wb") as stream:
            plistlib.dump(job, stream)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "environment is invalid"):
                self.ensure()

    def test_missing_or_invalid_executables_fail_before_install(self):
        with mock.patch.object(t.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "not found in PATH"):
                t.render(self.workspace, self.home)
        with self.assertRaisesRegex(ValueError, "python executable is not runnable"):
            t.render(self.workspace, self.home, codex_bin=self.codex,
                     python=self.root / "missing-python")
        self.assertEqual(self.launchctl.calls, [])

    def test_stale_or_invalid_plist_is_refused_instead_of_reenabled(self):
        self.ensure(enabled_override="0")
        dest = t.plist_path(self.home, self.agents)
        job = self.read_job()
        job["Label"] = "wrong-label"
        with open(dest, "wb") as stream:
            plistlib.dump(job, stream)
        self.assertIn("error", t.status(self.home, launch_agents=self.agents,
                                        runner=self.launchctl))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "label does not match"):
                self.ensure()
        job["Label"] = t.label_for(self.home)
        job["EnvironmentVariables"]["SUTANDO_CODEX_AUTO_RESET_ENABLED"] = 0
        with open(dest, "wb") as stream:
            plistlib.dump(job, stream)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "enable flag is invalid"):
                self.ensure()

    def test_bootout_failures_are_loud(self):
        def rejected(argv):
            if argv[1] == "print":
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 5, "", "bootout rejected")

        with self.assertRaisesRegex(RuntimeError, "bootout failed rc=5"):
            t._bootout(self.home, runner=rejected)

        def stuck(argv):
            return subprocess.CompletedProcess(argv, 0, "", "")

        with self.assertRaisesRegex(RuntimeError, "remained loaded"):
            t._bootout(self.home, runner=stuck, sleep=lambda _: None)

    def test_nonmac_cli_only_allows_noop_ensure(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(t.sys, "platform", "linux"), redirect_stdout(stdout):
            self.assertEqual(t.main(["ensure", "--workspace", str(self.workspace),
                                     "--codex-home", str(self.home)]), 0)
        self.assertIn("unavailable", stdout.getvalue())
        with mock.patch.object(t.sys, "platform", "linux"), redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                t.main(["status", "--codex-home", str(self.home)])
        self.assertIn("requires macOS", stderr.getvalue())

    def test_missing_workspace_or_unix_lock_refuses_install(self):
        error = io.StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit):
            t.main(["install", "--codex-home", str(self.home)])
        self.assertIn("needs --workspace", error.getvalue())
        with mock.patch.object(t, "fcntl", None):
            with self.assertRaisesRegex(RuntimeError, "requires Unix file locking"):
                t.install(self.workspace, self.home, codex_bin=self.codex,
                          launch_agents=self.agents, runner=self.launchctl)

    def test_default_launchctl_transport_uses_captured_subprocess(self):
        answer = subprocess.CompletedProcess(["launchctl", "print", "test"], 113, "", "")
        with mock.patch.object(t.subprocess, "run", return_value=answer) as run:
            self.assertIs(t._launchctl(["print", "test"]), answer)
        run.assert_called_once_with(["launchctl", "print", "test"],
                                    capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
