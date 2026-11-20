#!/usr/bin/env python3
"""A remedy timer an earlier install left with the standby's alert settings in its job.

One release saved SUTANDO_NOTIFIER_ALERT_GRACE/REPEAT/PILEUP in the timer job. They are no longer
saved, and nothing migrates them away: GRACE and REPEAT stay in force as owner settings (the sweep
hands them to each worker's supervisor ensure, which forwards them), the pile-up setting reaches no
supervisor and changes nothing, and an explicit reinstall writes the job without any of them.
The launchctl here is a fake; no real launchd or tmux server is touched.

Run: python3 tests/skills/worker-pool/pool-remedy-timer-old-alert-settings.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pool_remedy_timer as t  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sw = _load("spawn_worker")
rem = _load("pool_remedy")

SAVED = {"SUTANDO_NOTIFIER_ALERT_GRACE": "60", "SUTANDO_NOTIFIER_ALERT_REPEAT": "600",
         "SUTANDO_NOTIFIER_ALERT_PILEUP": "5"}
WID = "0123456789abcdef0123456789abcdef"


class Launchctl:
    def __init__(self):
        self.loaded, self.calls = set(), []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        cp = subprocess.CompletedProcess
        verb, target = argv[1], argv[-1]
        if verb == "print":
            return cp(argv, 0 if target in self.loaded else 113, "", "")
        if verb == "bootout":
            self.loaded.discard(target)
        elif verb == "bootstrap":
            with open(target, "rb") as fh:
                self.loaded.add(f"gui/{os.getuid()}/{plistlib.load(fh)['Label']}")
        return cp(argv, 0, "", "")


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "", "")


class OldTimerSettings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name) / "ws"
        (self.ws / "state" / "workers" / WID).mkdir(parents=True)
        (self.ws / "state" / "roster.json").write_text(json.dumps({"workers": {WID: {"runtime": "claude", "state": "live"}}}))
        (self.ws / "state" / "workers" / WID / "incarnations.json").write_text(
            json.dumps({"incarnations": [{"tmux": {"socket": str(Path(tmp.name) / "no-such.sock")}}]}))
        self.la = Path(tmp.name) / "LaunchAgents"
        self.la.mkdir()
        self.lc = Launchctl()
        job = t.render(self.ws, REPO, stop_interrupt="1")
        job["EnvironmentVariables"].update(SAVED)
        self.plist = t.plist_path(self.ws, self.la)
        with open(self.plist, "wb") as fh:
            plistlib.dump(job, fh)
        self.lc(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(self.plist)])
        self.lc.calls.clear()
        self.saved_env = dict(job["EnvironmentVariables"])

    def plist_env(self):
        with open(self.plist, "rb") as fh:
            return plistlib.load(fh)["EnvironmentVariables"]

    def test_a_spawn_leaves_the_saved_settings_alone(self):
        before = self.plist.read_bytes()
        platform, sys.platform = sys.platform, "darwin"
        try:
            with mock.patch.dict(os.environ, {"SUTANDO_STOP_INTERRUPT": "1"}):
                out = sw.ensure_remedy_timer(self.ws, str(REPO), runner=self.lc, launch_agents=self.la)
        finally:
            sys.platform = platform
        self.assertEqual(out.get("why"), "already installed", out)
        self.assertEqual(self.plist.read_bytes(), before, "nothing migrates them away")
        self.assertNotIn("bootout", [c[1] for c in self.lc.calls])

    def test_the_sweep_keeps_grace_and_repeat_in_force_and_the_pileup_setting_reaches_no_supervisor(self):
        run = Runner()
        # launchd starts the sweep with exactly the job's environment.
        with mock.patch.dict(os.environ, self.saved_env, clear=True):
            rem.ensure_supervisor(self.ws, REPO, WID, runner=run)
        ensure_env = run.calls[0][1]["env"]
        self.assertEqual({k: ensure_env.get(k) for k in SAVED}, SAVED, "the sweep hands every saved setting on")
        printed = subprocess.run(["bash", str(SCRIPTS / "worker-watcher-supervisor.sh"), "--print-command"],
                                 env=ensure_env, capture_output=True, text=True, timeout=60)
        self.assertEqual(printed.returncode, 0, printed.stderr)
        lines = printed.stdout.splitlines()
        self.assertIn("SUTANDO_NOTIFIER_ALERT_GRACE=60", lines)
        self.assertIn("SUTANDO_NOTIFIER_ALERT_REPEAT=600", lines)
        self.assertFalse([l for l in lines if "PILEUP" in l], "the pile-up setting is not forwarded")

    def test_the_supervisor_reads_grace_and_repeat_and_ignores_the_pileup_setting(self):
        script = REPO / "src" / "agent" / "codex" / "cli" / "task-notifier-supervisor.sh"

        def settings(env):
            out = subprocess.run(["bash", "-c", 'source "$1"; printf "%s %s" "$ALERT_GRACE" "$ALERT_REPEAT"', "_", str(script)],
                                 env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
                                      "SUTANDO_SUPERVISOR_SOURCE_ONLY": "1", **env},
                                 capture_output=True, text=True, timeout=60)
            return out.stdout
        self.assertEqual(settings(SAVED), "60 600")
        self.assertEqual(settings({"SUTANDO_NOTIFIER_ALERT_PILEUP": "5"}), "300 0", "the pile-up setting changes nothing")

    def test_an_explicit_reinstall_writes_the_job_without_them_and_keeps_the_stop_setting(self):
        # Even an installer whose own shell sets them does not save them.
        with mock.patch.dict(os.environ, {"SUTANDO_NOTIFIER_ALERT_GRACE": "90"}):
            t.install(self.ws, REPO, launch_agents=self.la, runner=self.lc, sleep=lambda s: None)
        env = self.plist_env()
        self.assertFalse([k for k in env if k.startswith("SUTANDO_NOTIFIER_ALERT_")], env)
        self.assertEqual(env.get("SUTANDO_STOP_INTERRUPT"), "1")
        self.assertIn(f"gui/{os.getuid()}/{t.label_for(self.ws)}", self.lc.loaded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
