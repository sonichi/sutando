#!/usr/bin/env python3
"""claude-hooks probe: the core's launch settings are the only registration.

It must warn when the launch JSON would miss an owned or skill hook, point at a
missing script, or leave transcripts on the 30-day default, and when a settings
file still holds a Sutando-written copy that fires outside the core launch.

Run: python3 tests/health-check-claude-hook-registration.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
BUILDER = "src/agent/claude/cli/build-core-settings.mjs"
OWNED = ("check-pending-tasks.sh", "turn-start.sh", "session-handoff.sh",
         "schedule-crons-session-hint.sh", "personal-claude-compact-hint.sh",
         "watcher-rearm-session-hint.sh")


def _load():
    spec = importlib.util.spec_from_file_location("hc_hooks_test", REPO / "src" / "health-check.py")
    hc = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(hc)
    except SystemExit:
        pass
    return hc


hc = _load()


@unittest.skipUnless(shutil.which("node"), "node is required to run the launch-settings builder")
class Probe(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.repo = Path(self._td.name) / "repo"
        (self.repo / BUILDER).parent.mkdir(parents=True)
        shutil.copy(REPO / BUILDER, self.repo / BUILDER)
        for name in OWNED:
            (self.repo / "src" / name).write_text("#!/bin/bash\n")
        (self.repo / "hooks").mkdir()
        (self.repo / "hooks" / "skip-ask-user-question.py").write_text("")
        skill = self.repo / "skills" / "demo"
        (skill / "hooks").mkdir(parents=True)
        (skill / "hooks" / "g.py").write_text("")
        (skill / "manifest.json").write_text(json.dumps(
            {"name": "demo", "hooks": [{"event": "PreToolUse", "command": "./hooks/g.py"}]}))
        self.ccd = Path(self._td.name) / "ccd"
        self.ccd.mkdir()
        self._env = mock.patch.dict(os.environ, {"SUTANDO_CLAUDE_WORKING_DIR": ""})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._td.cleanup()

    def probe(self, **kw):
        return hc.check_claude_hook_registration(repo_dir=self.repo, config_dir=self.ccd, **kw)

    def test_ok_when_the_launch_settings_carry_everything_and_no_file_has_a_copy(self):
        out = self.probe()
        self.assertEqual(out["status"], "ok", out["detail"])
        # guard + 7 owned + 1 skill hook
        self.assertIn("9 hooks registered by the core's launch settings only", out["detail"])

    def test_not_a_checkout(self):
        os.remove(self.repo / BUILDER)
        self.assertIn("not a sutando checkout", self.probe()["detail"])

    def test_node_missing_warns(self):
        with mock.patch.object(hc.shutil, "which", return_value=None):
            out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("node not found", out["detail"])

    def test_a_failing_builder_warns_with_its_exit(self):
        (self.repo / BUILDER).write_text("process.stderr.write('boom'); process.exit(4);\n")
        out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("exited 4", out["detail"])

    def test_a_builder_that_cannot_run_warns(self):
        with mock.patch.object(hc.subprocess, "run", side_effect=OSError("no exec")):
            out = self.probe()
        self.assertIn("build-core-settings.mjs failed", out["detail"])

    def test_a_raising_discovery_warns(self):
        import skill_hooks
        with mock.patch.object(skill_hooks, "discover", side_effect=RuntimeError("boom")):
            out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("skill-hook discovery failed", out["detail"])

    def test_a_builder_missing_events_skill_hooks_and_retention_warns_for_each(self):
        (self.repo / BUILDER).write_text(
            'process.stdout.write(JSON.stringify({hooks: {Stop: [{hooks: [{type: "command", command: "true"}]}]}}));\n')
        detail = self.probe()["detail"]
        self.assertIn("lack UserPromptSubmit, PreCompact, SessionEnd, SessionStart hooks", detail)
        self.assertIn("skill hooks missing from launch settings: PreToolUse:", detail)
        self.assertIn("cleanupPeriodDays", detail)

    def test_an_owned_script_that_is_gone_warns(self):
        os.remove(self.repo / "src" / "turn-start.sh")
        out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn(f"missing scripts: {self.repo / 'src' / 'turn-start.sh'}", out["detail"])

    def test_copies_in_the_project_file_and_config_dir_are_reported_for_the_fix(self):
        proj = self.repo / ".claude" / "settings.json"
        proj.parent.mkdir()
        proj.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": f"bash '{self.repo}/src/check-pending-tasks.sh'"},
            {"type": "command", "command": "echo operator"}]}]}}))
        (self.ccd / "settings.json").write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [
            {"type": "command", "command": f'bash "{self.repo}/src/session-handoff.sh" "${{TRANSCRIPT_PATH:-}}"'}]}]}}))
        out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("2 Sutando hook copies still in a settings file", out["detail"])
        self.assertEqual(len(out["_project_leftovers"]), 2)
        self.assertNotIn("echo operator", " ".join(out["_project_leftovers"]))
        # Probing is read-only.
        self.assertIn("check-pending-tasks.sh", proj.read_text())

    def test_one_copy_is_singular(self):
        (self.ccd / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": f"bash '{self.repo}/src/check-pending-tasks.sh'"}]}]}}))
        self.assertIn("1 Sutando hook copy still", self.probe()["detail"])

    def test_an_unreadable_settings_file_warns_instead_of_raising(self):
        (self.ccd / "settings.json").write_text("{nope")
        out = self.probe()
        self.assertEqual(out["status"], "warn")
        self.assertIn("could not read a settings file", out["detail"])

    def test_the_default_config_dir_comes_from_the_shared_resolver(self):
        ws = Path(self._td.name) / "ws"
        (ws / ".claude-sutando").mkdir(parents=True)
        (ws / ".claude-sutando" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": f"bash '{self.repo}/src/check-pending-tasks.sh'"}]}]}}))
        with mock.patch.dict(os.environ, {"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws)}):
            out = hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(len(out["_project_leftovers"]), 1, out["detail"])


@unittest.skipUnless(shutil.which("node"), "node is required to run the launch-settings builder")
class LiveTree(unittest.TestCase):
    def test_this_checkout_builds_launch_settings_with_every_owned_hook(self):
        settings, why = hc._core_launch_settings(REPO)
        self.assertIsNotNone(settings, why)
        commands = " ".join(h["command"] for gs in settings["hooks"].values() for g in gs for h in g["hooks"])
        for name in OWNED:
            self.assertIn(f"/src/{name}", commands)
        self.assertEqual(settings["cleanupPeriodDays"], 3650)


if __name__ == "__main__":
    unittest.main(verbosity=2)
