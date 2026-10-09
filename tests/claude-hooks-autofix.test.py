"""--fix for claude-hooks sweeps Sutando-written copies out of settings files, keyed
on the probe's structured `_project_leftovers`, and re-runs the probe afterwards.
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


hc = _load()


class Gating(unittest.TestCase):
    def _run(self, check, run=None):
        calls, buf = [], io.StringIO()

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="claude-hooks sweep: 1 owned entry removed\n", stderr="")

        fresh = {"name": "claude-hooks", "status": "ok", "detail": "fresh"}
        with mock.patch.object(hc.subprocess, "run", side_effect=run or fake_run), \
                mock.patch.object(hc, "check_claude_hook_registration", return_value=dict(fresh)):
            checks = [check]
            hc.apply_claude_hooks_fix(checks, stream=buf)
        return calls, checks[0], buf.getvalue()

    def test_leftovers_run_the_sweep_once_and_the_probe_is_rerun(self):
        calls, after, out = self._run({"name": "claude-hooks", "status": "warn", "detail": "x",
                                       "_project_leftovers": ["/p: Stop bash x"]})
        self.assertEqual(len(calls), 1)
        self.assertTrue(str(calls[0][-1]).endswith("src/install-claude-hooks.sh"), calls)
        self.assertEqual(after["detail"], "fresh")
        self.assertIn("sweeping 1 owned entry", out)
        self.assertIn("1 owned entry removed", out)

    def test_plural_and_silent_sweep_output(self):
        def quiet(argv, **kw):
            return subprocess.CompletedProcess(argv, 3, stdout="", stderr="")
        _calls, _after, out = self._run({"name": "claude-hooks", "status": "warn", "detail": "x",
                                         "_project_leftovers": ["a", "b"]}, run=quiet)
        self.assertIn("sweeping 2 owned entries", out)
        self.assertIn("sweep exited 3", out)

    def test_a_sweep_that_cannot_start_warns_instead_of_raising(self):
        _calls, _after, out = self._run({"name": "claude-hooks", "status": "warn", "detail": "x",
                                         "_project_leftovers": ["a"]}, run=OSError("nope"))
        self.assertIn("could not run install-claude-hooks.sh", out)

    def test_warns_the_sweep_cannot_repair_are_left_alone(self):
        for check in ({"name": "claude-hooks", "status": "warn", "detail": "node not found",
                       "_project_leftovers": []},
                      {"name": "other", "status": "warn", "detail": "x", "_project_leftovers": ["a"]}):
            calls, after, _ = self._run(dict(check))
            self.assertEqual(calls, [])
            self.assertEqual(after["detail"], check["detail"])


@unittest.skipUnless(shutil.which("node"), "node is required to run the launch-settings builder")
class EndToEnd(unittest.TestCase):
    def test_fix_sweeps_a_real_project_file_and_the_probe_turns_ok(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            for rel in ("src/install-claude-hooks.sh", "src/claude_hooks_settings.py", "src/skill_hooks.py",
                        "src/sutando_config.py", "scripts/python-binary.sh",
                        "src/agent/claude/cli/build-core-settings.mjs"):
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(REPO / rel, repo / rel)
            for name in ("check-pending-tasks.sh", "turn-start.sh", "session-handoff.sh",
                         "schedule-crons-session-hint.sh", "personal-claude-compact-hint.sh",
                         "watcher-rearm-session-hint.sh"):
                (repo / "src" / name).write_text("#!/bin/bash\n")
            (repo / "hooks").mkdir()
            (repo / "hooks" / "skip-ask-user-question.py").write_text("")
            proj = repo / ".claude" / "settings.json"
            proj.parent.mkdir()
            proj.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": f"bash '{repo}/src/check-pending-tasks.sh'"},
                {"type": "command", "command": "echo operator"}]}]}}))
            env = {"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(Path(td) / "ws"),
                   "SUTANDO_CLAUDE_WORKING_DIR": ""}
            with mock.patch.dict(os.environ, env), mock.patch.object(hc, "REPO_DIR", repo):
                checks = [hc.check_claude_hook_registration()]
                self.assertEqual(checks[0]["status"], "warn", checks[0]["detail"])
                hc.apply_claude_hooks_fix(checks, stream=io.StringIO())
            self.assertEqual(checks[0]["status"], "ok", checks[0]["detail"])
            left = [h["command"] for g in json.loads(proj.read_text())["hooks"]["Stop"] for h in g["hooks"]]
            self.assertEqual(left, ["echo operator"])


class DispatchWiring(unittest.TestCase):
    def test_fix_dispatch_calls_the_handler(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("apply_claude_hooks_fix(checks, stream=", src,
                      "handler is defined but never dispatched — --fix would be inert")


if __name__ == "__main__":
    unittest.main(verbosity=2)
