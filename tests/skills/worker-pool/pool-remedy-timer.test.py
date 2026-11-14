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
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_remedy_timer as t  # noqa: E402


class FakeLaunchctl:
    """`print` answers from a set of loaded labels; bootstrap/bootout move labels
    in and out of it. Records every argv."""

    def __init__(self, bootstrap_fails=False, fail_labels=()):
        self.loaded, self.calls, self.bootstrap_fails = set(), [], bootstrap_fails
        self.fail_labels = set(fail_labels)

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
            with open(target, "rb") as fh:
                label = plistlib.load(fh)["Label"]
            if self.bootstrap_fails or label in self.fail_labels:
                return cp(argv, 5, "", "Bootstrap failed: 5: Input/output error")
            self.loaded.add(f"gui/{__import__('os').getuid()}/{label}")
            return cp(argv, 0, "", "")
        return cp(argv, 0, "", "")

    def verbs(self):
        return [a[1] for a in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.la = self.ws / "LaunchAgents"
        self.lc = FakeLaunchctl()

    def install(self, **kw):
        return t.install(self.ws, REPO, launch_agents=self.la, runner=self.lc,
                         sleep=lambda s: None, **kw)

    def legacy(self, workspace):
        job = t.render(workspace, REPO)
        job["Label"] = t.LABEL
        path = t.legacy_plist_path(self.la)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            plistlib.dump(job, fh)
        self.lc(["launchctl", "bootstrap", f"gui/{__import__('os').getuid()}", str(path)])
        return path


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
        self.assertEqual(job["Label"], t.label_for(self.ws))
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
        self.assertEqual(t.status(self.ws, launch_agents=self.la, runner=self.lc)["interval_s"], 120)
        self.assertEqual(len(list(self.la.glob("*.plist"))), 1)

    def test_uninstall_removes_the_job_and_the_plist_and_is_repeatable(self):
        self.install()
        out = t.uninstall(self.ws, launch_agents=self.la, runner=self.lc, sleep=lambda s: None)
        self.assertTrue(out["removed"])
        self.assertFalse(out["loaded"])
        again = t.uninstall(self.ws, launch_agents=self.la, runner=self.lc, sleep=lambda s: None)
        self.assertFalse(again["removed"])


class WorkspaceIsolation(Base):
    def test_two_workspaces_have_independent_jobs(self):
        other = self.ws / "other"
        first = self.install()
        second = t.install(other, REPO, launch_agents=self.la, runner=self.lc,
                           sleep=lambda s: None)
        self.assertNotEqual(first["label"], second["label"])
        self.assertNotEqual(first["plist"], second["plist"])
        self.assertTrue(t.status(self.ws, launch_agents=self.la, runner=self.lc)["loaded"])
        self.assertTrue(t.status(other, launch_agents=self.la, runner=self.lc)["loaded"])
        t.uninstall(other, launch_agents=self.la, runner=self.lc, sleep=lambda s: None)
        self.assertTrue(t.status(self.ws, launch_agents=self.la, runner=self.lc)["loaded"])
        self.assertFalse(t.status(other, launch_agents=self.la, runner=self.lc)["installed"])

    def test_matching_legacy_singleton_migrates_without_duplicate_sweeper(self):
        old = self.legacy(self.ws)
        out = self.install()
        self.assertTrue(out["legacy_migrated"])
        self.assertFalse(old.exists())
        self.assertFalse(t.legacy_status(launch_agents=self.la, runner=self.lc)["loaded"])
        self.assertTrue(t.status(self.ws, launch_agents=self.la, runner=self.lc)["loaded"])

    def test_explicit_install_restores_a_legacy_timer_bound_to_another_repo(self):
        old = self.legacy(self.ws)
        with open(old, "rb") as fh:
            job = plistlib.load(fh)
        job["ProgramArguments"][job["ProgramArguments"].index("--repo") + 1] = "/dev/checkout"
        with open(old, "wb") as fh:
            plistlib.dump(job, fh)
        out = self.install()
        self.assertTrue(out["legacy_migrated"])
        self.assertFalse(old.exists())
        self.assertEqual(t.status(self.ws, launch_agents=self.la, runner=self.lc)["repo"],
                         str(REPO))

    def test_other_workspaces_legacy_singleton_is_left_running(self):
        old = self.legacy(self.ws / "other")
        out = self.install()
        self.assertFalse(out["legacy_migrated"])
        self.assertTrue(old.exists())
        self.assertTrue(t.legacy_status(launch_agents=self.la, runner=self.lc)["loaded"])
        self.assertTrue(t.status(self.ws, launch_agents=self.la, runner=self.lc)["loaded"])

    def test_a_failed_migration_restores_the_legacy_job(self):
        old = self.legacy(self.ws)
        self.lc.fail_labels.add(t.label_for(self.ws))
        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            self.install()
        self.assertTrue(old.exists())
        self.assertTrue(t.legacy_status(launch_agents=self.la, runner=self.lc)["loaded"])
        self.assertFalse(t.status(self.ws, launch_agents=self.la, runner=self.lc)["installed"])

    def test_uninstall_retires_a_matching_legacy_timer_too(self):
        old = self.legacy(self.ws)
        self.lc.calls.clear()
        out = t.uninstall(self.ws, launch_agents=self.la, runner=self.lc,
                          sleep=lambda s: None)
        self.assertTrue(out["removed"])
        self.assertFalse(old.exists())
        self.assertFalse(t.legacy_status(launch_agents=self.la, runner=self.lc)["loaded"])
        self.assertIn(["launchctl", "bootout", t.legacy_service_target()], self.lc.calls)


class Failures(Base):
    def test_import_without_fcntl_explains_why_a_timer_cannot_be_locked(self):
        real_import = __import__

        def without_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                raise ImportError("Unix locking unavailable")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=without_fcntl):
            portable = runpy.run_path(str(Path(t.__file__)))
        with self.assertRaisesRegex(RuntimeError, "requires Unix file locking"):
            with portable["timer_lock"](self.la):
                self.fail("a platform without fcntl acquired a timer lock")
        self.assertFalse(self.la.exists(), "a refused lock must not create LaunchAgents")

    def test_bootout_failure_is_reported_and_preserves_the_loaded_job(self):
        self.install()
        target = t.service_target(self.ws)

        def refusing_bootout(argv, **kw):
            if argv == ["launchctl", "bootout", target]:
                return subprocess.CompletedProcess(argv, 5, "", "permission denied")
            return self.lc(argv, **kw)

        with self.assertRaisesRegex(RuntimeError, "bootout failed rc=5: permission denied"):
            t.bootout(self.ws, refusing_bootout, sleep=lambda s: None)
        self.assertIn(target, self.lc.loaded)

    def test_bootout_waits_for_an_asynchronous_unload_before_returning(self):
        self.install()
        lingering = {"n": 2}                      # `print` keeps answering "loaded" twice
        real = self.lc.__call__

        def slow(argv, **kw):
            r = real(argv, **kw)
            if argv[1] == "print" and lingering["n"] > 0 and r.returncode != 0:
                lingering["n"] -= 1
                return subprocess.CompletedProcess(argv, 0, "", "")
            return r
        naps = []
        t.bootout(self.ws, slow, sleep=naps.append)
        self.assertEqual(len(naps), 2, "bootout returned before launchd reported the job gone")

    def test_bootout_refuses_to_bootstrap_over_a_job_that_never_unloads(self):
        self.install()
        target = t.service_target(self.ws)
        naps = []

        def stuck(argv, **kw):
            if argv == ["launchctl", "print", target]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return self.lc(argv, **kw)

        with self.assertRaisesRegex(RuntimeError, "remained loaded after bootout"):
            t.bootout(self.ws, stuck, sleep=naps.append)
        self.assertEqual(len(naps), 10, "bootout must stop after its bounded wait")

    def test_status_reports_an_unreadable_plist_instead_of_guessing(self):
        t.plist_path(self.ws, self.la).parent.mkdir(parents=True)
        t.plist_path(self.ws, self.la).write_text("not a plist")
        st = t.status(self.ws, launch_agents=self.la, runner=self.lc)
        self.assertTrue(st["installed"])
        self.assertIn("plist unreadable", st["error"])
        self.assertNotIn("interval_s", st)

    def test_status_refuses_a_plist_whose_label_does_not_match_its_filename(self):
        dest = t.plist_path(self.ws, self.la)
        dest.parent.mkdir(parents=True)
        job = t.render(self.ws, REPO)
        job["Label"] = "com.sutando.pool-remedy.someone-else"
        with open(dest, "wb") as fh:
            plistlib.dump(job, fh)
        st = t.status(self.ws, launch_agents=self.la, runner=self.lc)
        self.assertIn("does not match", st["error"])
        self.assertNotIn("repo", st, "a mislabeled job must not be trusted as this timer")

    def test_a_bootstrap_failure_is_an_error_not_an_installed_looking_job(self):
        self.lc.bootstrap_fails = True
        with self.assertRaises(RuntimeError) as cm:
            self.install()
        self.assertIn("Input/output error", str(cm.exception))
        self.assertFalse(t.status(self.ws, launch_agents=self.la, runner=self.lc)["loaded"])

    def test_failed_reinstall_restores_the_old_plist_and_running_job(self):
        old = self.install()
        dest = Path(old["plist"])
        old_bytes = dest.read_bytes()
        failed = False

        def fail_one_bootstrap(argv, **kw):
            nonlocal failed
            if argv[1] == "bootstrap" and not failed:
                failed = True
                return subprocess.CompletedProcess(argv, 5, "", "temporary bootstrap failure")
            return self.lc(argv, **kw)

        with self.assertRaisesRegex(RuntimeError, "temporary bootstrap failure"):
            t.install(self.ws, REPO, interval_s=120, launch_agents=self.la,
                      runner=fail_one_bootstrap, sleep=lambda s: None)
        self.assertTrue(failed)
        self.assertEqual(dest.read_bytes(), old_bytes)
        self.assertTrue(t.status(self.ws, launch_agents=self.la, runner=self.lc)["loaded"])
        self.assertEqual(t.status(self.ws, launch_agents=self.la, runner=self.lc)["interval_s"], 300)

    def test_failed_restore_of_existing_plist_and_job_is_reported(self):
        old = self.install()
        dest = Path(old["plist"])
        real_replace = t.os.replace
        replaces = 0

        def fail_second_replace(src, dst):
            nonlocal replaces
            if Path(dst) == dest:
                replaces += 1
                if replaces == 2:
                    raise OSError("old plist could not be restored")
            return real_replace(src, dst)

        self.lc.bootstrap_fails = True
        with mock.patch.object(t.os, "replace", side_effect=fail_second_replace):
            with self.assertRaisesRegex(RuntimeError, "rollback failed") as cm:
                self.install(interval_s=120)
        self.assertIn("old plist could not be restored", str(cm.exception))
        self.assertIn("could not restore", str(cm.exception))
        self.assertFalse(t.status(self.ws, launch_agents=self.la, runner=self.lc)["loaded"])

    def test_failed_restore_of_legacy_plist_reports_the_missing_job(self):
        legacy = self.legacy(self.ws)
        self.lc.fail_labels.add(t.label_for(self.ws))
        real_replace = t.os.replace

        def fail_legacy_restore(src, dst):
            if str(src).endswith(".migrating") and Path(dst) == legacy:
                raise OSError("legacy plist could not be restored")
            return real_replace(src, dst)

        with mock.patch.object(t.os, "replace", side_effect=fail_legacy_restore):
            with self.assertRaisesRegex(RuntimeError, "rollback failed") as cm:
                self.install()
        self.assertIn("legacy plist could not be restored", str(cm.exception))
        self.assertIn("plist missing", str(cm.exception))
        self.assertFalse(legacy.exists())
        self.assertEqual(len(list(self.la.glob("*.migrating"))), 1)


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
        self.assertEqual(self._run("status")[0], 2)
        self.assertEqual(self._run("uninstall")[0], 2)

    def test_a_refused_interval_is_a_nonzero_exit_with_the_reason_on_stderr(self):
        rc, _, err = self._run("install", "--workspace", str(self.ws), "--repo", str(REPO),
                               "--interval", "30")
        self.assertEqual(rc, 1)
        self.assertIn("interval must be >= 60s", err)
        self.assertFalse(t.plist_path(self.ws, self.la).exists(), "a refused install wrote a plist")

    def test_uninstall_from_the_command_line(self):
        self._run("install", "--workspace", str(self.ws), "--repo", str(REPO))
        rc, out, _ = self._run("uninstall", "--workspace", str(self.ws))
        self.assertEqual(rc, 0)
        self.assertIn("removed: True", out)
        self.assertIn("bootout", [a[1] for a in self.lc.calls])

    def test_status_before_and_after(self):
        rc, out, _ = self._run("status", "--workspace", str(self.ws))
        self.assertEqual(rc, 0)
        self.assertIn("installed: False", out)
        rc, out, _ = self._run("install", "--workspace", str(self.ws), "--repo", str(REPO))
        self.assertEqual(rc, 0)
        self.assertIn("loaded: True", out)
        self.assertIn("bootstrap", [a[1] for a in self.lc.calls],
                      "the CLI reached the REAL launchctl: a def-time `runner=subprocess.run` "
                      "default ignores the patch and bootstraps a temp plist under the live label")
        rc, out, _ = self._run("status", "--workspace", str(self.ws))
        self.assertIn("installed: True", out)
        self.assertIn("interval_s: 300", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
