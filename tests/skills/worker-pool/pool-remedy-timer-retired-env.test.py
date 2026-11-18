#!/usr/bin/env python3
"""A remedy timer installed while it saved the standby's alert settings is migrated once.

An earlier install wrote SUTANDO_NOTIFIER_ALERT_GRACE/REPEAT/PILEUP into the timer job, and the
sweep handed them to every worker's supervisor ensure. They are no longer saved, so a host that
upgrades must lose them from the plist and from the loaded job, keeping every other entry. The
launchctl here is a fake that reports each loaded job's environment; no real launchd is touched.

Run: python3 tests/skills/worker-pool/pool-remedy-timer-retired-env.test.py
"""
from __future__ import annotations

import importlib.util
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pool_remedy_timer as t  # noqa: E402

RETIRED = {"SUTANDO_NOTIFIER_ALERT_GRACE": "60", "SUTANDO_NOTIFIER_ALERT_REPEAT": "0",
           "SUTANDO_NOTIFIER_ALERT_PILEUP": "5"}
KEPT = {"SUTANDO_STOP_INTERRUPT": "1", "SOME_OTHER_SETTING": "kept"}


class Launchctl:
    """`print` answers for loaded labels with their environment, as launchctl prints it;
    bootstrap loads a plist's label and env (or fails `fail_bootstrap` times); bootout unloads."""

    def __init__(self, fail_bootstrap=0, fail_bootout=False):
        self.loaded: dict = {}
        self.calls: list = []
        self.fail_bootstrap, self.fail_bootout = fail_bootstrap, fail_bootout

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        cp = subprocess.CompletedProcess
        verb, target = argv[1], argv[-1]
        if verb == "print":
            if target not in self.loaded:
                return cp(argv, 113, "", "Could not find service")
            env = "".join(f"\t\t{k} => {v}\n" for k, v in self.loaded[target].items())
            return cp(argv, 0, f"{target} = {{\n\tenvironment = {{\n{env}\t}}\n}}\n", "")
        if verb == "bootout":
            if self.fail_bootout:
                return cp(argv, 5, "", "Boot-out failed: 5: Input/output error")
            self.loaded.pop(target, None)
            return cp(argv, 0, "", "")
        if verb == "bootstrap":
            if self.fail_bootstrap:
                self.fail_bootstrap -= 1
                return cp(argv, 5, "", "Bootstrap failed: 5: Input/output error")
            with open(target, "rb") as fh:
                job = plistlib.load(fh)
            self.loaded[f"gui/{os.getuid()}/{job['Label']}"] = dict(job.get("EnvironmentVariables") or {})
            return cp(argv, 0, "", "")
        return cp(argv, 0, "", "")

    def verbs(self):
        return [c[1] for c in self.calls if c[1] != "print"]


class Migration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name) / "ws"
        self.ws.mkdir()
        self.la = Path(tmp.name) / "LaunchAgents"
        self.la.mkdir()
        self.lc = Launchctl()

    def old_timer(self, *, label=None, dest=None, loaded=True, workspace=None, env_extra=RETIRED):
        """A timer as the earlier install left it: the retired keys beside the kept ones."""
        job = t.render(workspace or self.ws, REPO, stop_interrupt="1")
        if label:
            job["Label"] = label
        job["EnvironmentVariables"].update({**KEPT, **env_extra})
        dest = dest or t.plist_path(self.ws, self.la)
        with open(dest, "wb") as fh:
            plistlib.dump(job, fh)
        if loaded:
            self.lc(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(dest)])
        self.lc.calls.clear()
        return dest

    def env_of(self, dest):
        with open(dest, "rb") as fh:
            return plistlib.load(fh)["EnvironmentVariables"]

    def loaded_env(self, label):
        return self.lc.loaded.get(f"gui/{os.getuid()}/{label}")

    def migrate(self, **kw):
        return t.migrate_retired_env(self.ws, launch_agents=self.la, runner=self.lc, sleep=lambda s: None, **kw)

    def assert_clean(self, env):
        self.assertFalse(set(RETIRED) & set(env), env)
        self.assertEqual({k: env.get(k) for k in KEPT}, KEPT, "the Stop setting and other entries are kept")
        self.assertIn("PATH", env)

    def test_a_loaded_current_timer_is_rewritten_and_reloaded_once(self):
        dest = self.old_timer()
        out = self.migrate()
        self.assertEqual([(m["migrated"], m["reloaded"], m["removed"]) for m in out],
                         [(True, True, sorted(RETIRED))])
        self.assert_clean(self.env_of(dest))
        self.assert_clean(self.loaded_env(t.label_for(self.ws)))
        self.assertEqual(self.lc.verbs(), ["bootout", "bootstrap"])
        self.lc.calls.clear()
        before = dest.read_bytes()
        self.assertEqual(self.migrate(), [], "a second pass is a no-op")
        self.assertEqual((self.lc.verbs(), dest.read_bytes()), ([], before))

    def test_an_unloaded_current_timer_is_rewritten_and_left_unloaded(self):
        dest = self.old_timer(loaded=False)
        out = self.migrate()
        self.assertEqual([(m["migrated"], m["reloaded"]) for m in out], [(True, False)])
        self.assert_clean(self.env_of(dest))
        self.assertEqual(self.lc.verbs(), [])
        self.assertIsNone(self.loaded_env(t.label_for(self.ws)))
        self.assertEqual(self.migrate(), [])

    def test_a_loaded_job_still_carrying_the_keys_is_reloaded_from_its_clean_plist(self):
        dest = self.old_timer()
        with open(dest, "rb") as fh:
            job = plistlib.load(fh)
        job["EnvironmentVariables"] = {k: v for k, v in job["EnvironmentVariables"].items() if k not in RETIRED}
        with open(dest, "wb") as fh:
            plistlib.dump(job, fh)
        before = dest.read_bytes()
        out = self.migrate()
        self.assertEqual([(m["migrated"], m["reloaded"]) for m in out], [(True, True)])
        self.assertEqual(dest.read_bytes(), before, "a clean plist is not rewritten")
        self.assert_clean(self.loaded_env(t.label_for(self.ws)))
        self.assertEqual(self.migrate(), [])

    def test_the_legacy_label_bound_to_this_workspace_is_migrated_and_another_workspaces_is_not(self):
        legacy = self.old_timer(label=t.LABEL, dest=t.legacy_plist_path(self.la))
        out = self.migrate()
        self.assertEqual([(m["label"], m["migrated"]) for m in out], [(t.LABEL, True)])
        self.assert_clean(self.env_of(legacy))
        self.assert_clean(self.loaded_env(t.LABEL))
        other = Path(self.la.parent) / "other-ws"
        other.mkdir()
        legacy = self.old_timer(label=t.LABEL, dest=t.legacy_plist_path(self.la), workspace=other)
        before = legacy.read_bytes()
        self.assertEqual(self.migrate(), [])
        self.assertEqual((legacy.read_bytes(), self.lc.verbs()), (before, []), "another workspace's job is left alone")

    def test_a_failed_bootstrap_restores_the_plist_and_the_running_job_then_a_later_pass_migrates(self):
        dest = self.old_timer()
        before = dest.read_bytes()
        self.lc.fail_bootstrap = 1
        out = self.migrate()
        self.assertEqual([m["migrated"] for m in out], [False])
        self.assertIn("bootstrap failed", out[0]["why"])
        self.assertEqual(dest.read_bytes(), before, "the old plist is back")
        self.assertEqual(self.loaded_env(t.label_for(self.ws)), {**self.env_of(dest)}, "the old job runs again")
        self.assertEqual(self.lc.verbs(), ["bootout", "bootstrap", "bootstrap"])
        self.lc.calls.clear()
        self.assertEqual([m["migrated"] for m in self.migrate()], [True])
        self.assert_clean(self.loaded_env(t.label_for(self.ws)))

    def test_a_failed_bootout_restores_the_plist_and_leaves_the_job_running(self):
        dest = self.old_timer()
        before = dest.read_bytes()
        self.lc.fail_bootout = True
        out = self.migrate()
        self.assertEqual([m["migrated"] for m in out], [False])
        self.assertEqual(dest.read_bytes(), before)
        self.assertIn("SUTANDO_NOTIFIER_ALERT_GRACE", self.loaded_env(t.label_for(self.ws)))
        self.assertNotIn("bootstrap", self.lc.verbs())

    def test_inside_its_own_job_the_plist_is_cleaned_and_the_reload_left_to_the_next_pass(self):
        dest = self.old_timer()
        out = self.migrate(environ={"XPC_SERVICE_NAME": t.label_for(self.ws)})
        self.assertEqual([(m["migrated"], m["reloaded"]) for m in out], [(True, False)])
        self.assert_clean(self.env_of(dest))
        self.assertEqual(self.lc.verbs(), [], "never boots out the job it runs in")
        out = self.migrate(environ={})
        self.assertEqual([(m["migrated"], m["reloaded"]) for m in out], [(True, True)])
        self.assert_clean(self.loaded_env(t.label_for(self.ws)))

    def test_the_spawn_ensure_migrates_before_it_reads_the_timer_as_already_installed(self):
        spec = importlib.util.spec_from_file_location("spawn_worker", SCRIPTS / "spawn_worker.py")
        sw = importlib.util.module_from_spec(spec)
        sys.modules["spawn_worker"] = sw
        spec.loader.exec_module(sw)
        dest = self.old_timer()
        platform, sys.platform = sys.platform, "darwin"
        saved = os.environ.pop("SUTANDO_STOP_INTERRUPT", None)
        try:
            out = sw.ensure_remedy_timer(self.ws, str(REPO), runner=self.lc, launch_agents=self.la)
        finally:
            sys.platform = platform
            if saved is not None:
                os.environ["SUTANDO_STOP_INTERRUPT"] = saved
        self.assertEqual(out.get("why"), "already installed", out)
        self.assert_clean(self.env_of(dest))
        self.assert_clean(self.loaded_env(t.label_for(self.ws)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
