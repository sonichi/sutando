#!/usr/bin/env python3
"""The remedy timer installs one launchd job that runs the sweep, and nothing else.

What a timer nobody watches must get right: absolute paths recorded at install
time, a log under the workspace, a PATH that can find tmux, idempotent
re-install (boot out before bootstrap), and a bootstrap failure that is an
error, not a job that looks installed. launchctl is never called for real here.

Run: python3 tests/skills/worker-pool/pool-remedy-timer.test.py
"""
from __future__ import annotations

import contextlib
import io
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_remedy_timer as t  # noqa: E402


class FakeLaunchctl:
    """`print` answers from a set of loaded labels; bootstrap/bootout move labels
    in and out of it. Records every argv."""

    def __init__(self, bootstrap_fails=False):
        self.loaded, self.calls, self.bootstrap_fails = set(), [], bootstrap_fails

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        cp = subprocess.CompletedProcess
        verb, target = argv[1], argv[-1]
        if verb == "print":
            return cp(argv, 0 if target in self.loaded else 113, "", "")
        if verb == "bootout":
            self.loaded.discard(target)
            return cp(argv, 0, "", "")
        if verb == "bootstrap":
            if self.bootstrap_fails:
                return cp(argv, 5, "", "Bootstrap failed: 5: Input/output error")
            with open(target, "rb") as fh:
                self.loaded.add(f"gui/{__import__('os').getuid()}/{plistlib.load(fh)['Label']}")
            return cp(argv, 0, "", "")
        return cp(argv, 0, "", "")

    def verbs(self):
        return [a[1] for a in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.la = self.ws / "LaunchAgents"
        self.lc = FakeLaunchctl()

    def install(self, **kw):
        return t.install(self.ws, REPO, launch_agents=self.la, runner=self.lc,
                         sleep=lambda s: None, **kw)


class TheRenderedJob(Base):
    def test_it_runs_the_sweep_with_absolute_paths_and_logs_under_the_workspace(self):
        out = self.install()
        with open(out["plist"], "rb") as fh:
            job = plistlib.load(fh)
        args = job["ProgramArguments"]
        self.assertTrue(args[1].endswith("skills/worker-pool/scripts/pool_remedy.py"))
        self.assertTrue(Path(args[1]).is_absolute())
        self.assertEqual(args[2:], ["--workspace", str(self.ws.resolve()),
                                    "--repo", str(REPO), "--sweep"],
                         "the timer may only SWEEP; a recipient or dry-run here is a bug")
        self.assertEqual(job["StartInterval"], 300)
        self.assertEqual(job["StandardOutPath"], str(self.ws.resolve() / "logs" / "pool-remedy.log"))
        self.assertTrue((self.ws / "logs").is_dir(), "launchd does not create the log dir")
        tmux = shutil.which("tmux")
        if tmux:
            self.assertTrue(job["EnvironmentVariables"]["PATH"].startswith(str(Path(tmux).parent)),
                            "launchd's own PATH cannot find tmux; the job must carry a PATH that does")
        else:
            self.assertTrue(job["EnvironmentVariables"]["PATH"])

    def test_an_interval_under_a_minute_is_refused(self):
        with self.assertRaises(ValueError):
            t.render(self.ws, REPO, interval_s=30)

    def test_a_path_with_xml_specials_still_renders_a_valid_plist(self):
        ws = self.ws / "a&b <c>"
        ws.mkdir()
        out = t.install(ws, REPO, launch_agents=self.la, runner=self.lc, sleep=lambda s: None)
        with open(out["plist"], "rb") as fh:
            self.assertIn("a&b <c>", plistlib.load(fh)["ProgramArguments"][3])


class Idempotence(Base):
    def test_reinstall_boots_the_old_job_out_before_bootstrapping(self):
        self.install()
        self.install(interval_s=120)
        self.assertEqual(self.lc.verbs().count("bootstrap"), 2)
        first_boot = self.lc.verbs().index("bootout")
        second_bootstrap = [i for i, v in enumerate(self.lc.verbs()) if v == "bootstrap"][1]
        self.assertLess(first_boot, second_bootstrap, "bootstrap over a loaded job fails")
        self.assertEqual(t.status(launch_agents=self.la, runner=self.lc)["interval_s"], 120)
        self.assertEqual(len(list(self.la.glob("*.plist"))), 1)

    def test_uninstall_removes_the_job_and_the_plist_and_is_repeatable(self):
        self.install()
        out = t.uninstall(launch_agents=self.la, runner=self.lc, sleep=lambda s: None)
        self.assertTrue(out["removed"])
        self.assertFalse(out["loaded"])
        again = t.uninstall(launch_agents=self.la, runner=self.lc, sleep=lambda s: None)
        self.assertFalse(again["removed"])


class Failures(Base):
    def test_a_bootstrap_failure_is_an_error_not_an_installed_looking_job(self):
        self.lc.bootstrap_fails = True
        with self.assertRaises(RuntimeError) as cm:
            self.install()
        self.assertIn("Input/output error", str(cm.exception))
        self.assertFalse(t.status(launch_agents=self.la, runner=self.lc)["loaded"])


class TheCommandLine(Base):
    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        real = t.subprocess.run
        t.subprocess.run = self.lc
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    rc = t.main(["--launch-agents", str(self.la), *argv])
                except SystemExit as e:
                    rc = e.code
        finally:
            t.subprocess.run = real
        return rc, out.getvalue(), err.getvalue()

    def test_install_needs_both_paths(self):
        self.assertEqual(self._run("install", "--workspace", str(self.ws))[0], 2)

    def test_status_before_and_after(self):
        rc, out, _ = self._run("status")
        self.assertEqual(rc, 0)
        self.assertIn("installed: False", out)
        rc, out, _ = self._run("install", "--workspace", str(self.ws), "--repo", str(REPO))
        self.assertEqual(rc, 0)
        self.assertIn("loaded: True", out)
        rc, out, _ = self._run("status")
        self.assertIn("installed: True", out)
        self.assertIn("interval_s: 300", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
