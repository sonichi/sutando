#!/usr/bin/env python3
"""The activity hook is registered by the canonical settings builder, and it
writes only through the canonical workspace resolver (no write on failure).
Run: python3 tests/activity-hook-wiring.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class BuilderRegistersActivityHook(unittest.TestCase):
    def _build(self, *extra):
        out = subprocess.run(
            ["node", str(REPO / "src/agent/claude/cli/build-core-settings.mjs"),
             str(REPO / "hooks/skip-ask-user-question.py"), "",
             str(REPO / "hooks/skill-usage-telemetry.py"), *extra],
            capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_arg6_registers_posttooluse_on_every_tool(self):
        # argv[5] is the gmail write guard (main's slot); activity rides argv[6]
        d = self._build("", str(REPO / "hooks/emit-tool-activity.py"))
        ptu = d["hooks"]["PostToolUse"]
        acts = [e for e in ptu if "emit-tool-activity" in json.dumps(e)]
        self.assertEqual(len(acts), 1)
        self.assertNotIn("matcher", acts[0], "must fire for every tool")

    def test_omitted_arg_registers_nothing(self):
        d = self._build()
        self.assertNotIn("emit-tool-activity", json.dumps(d))

    def test_start_cli_passes_the_hook_path(self):
        s = (REPO / "src/agent/claude/cli/start-cli.sh").read_text()
        self.assertIn('"$REPO/hooks/emit-tool-activity.py"', s)


class HookUsesCanonicalWorkspaceOnly(unittest.TestCase):
    def _run(self, cwd, env_extra=None):
        env = dict(os.environ, **(env_extra or {}))
        return subprocess.run(
            [sys.executable, str(REPO / "hooks/emit-tool-activity.py")],
            input=json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": "true"}}),
            capture_output=True, text=True, cwd=cwd, env=env)

    def test_writes_into_the_resolved_workspace(self):
        # fixture repo skeleton: real resolver code, isolated tree — the
        # production hook must never be pointed at the checkout's live feed
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            (repo / "hooks").mkdir(parents=True)
            (repo / "src").mkdir()
            for mod in ("workspace_default.py", "sutando_config.py"):
                (repo / "src" / mod).write_text(
                    (REPO / "src" / mod).read_text())
            # the resolver anchors the repo root on this file's presence
            (repo / "sutando.config.json").write_text(
                (REPO / "sutando.config.json").read_text())
            hook = repo / "hooks" / "emit-tool-activity.py"
            hook.write_text((REPO / "hooks" / "emit-tool-activity.py").read_text())
            r = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps({"tool_name": "Bash",
                                  "tool_input": {"command": "true"}}),
                capture_output=True, text=True, cwd=td)
            self.assertEqual(r.returncode, 0, r.stderr)
            feed = repo / "workspace" / "state" / "activity-feed.jsonl"
            self.assertTrue(feed.exists(), "feed lands in the fixture tree")
            line = json.loads(feed.read_text().splitlines()[0])
            self.assertEqual(line.get("kind"), "tool")
            self.assertIn("step", line)

    def test_no_repo_means_no_write_anywhere(self):
        # hook copied outside any checkout: resolver unavailable -> exit 0, no file
        with tempfile.TemporaryDirectory() as td:
            hook = Path(td) / "emit-tool-activity.py"
            hook.write_text((REPO / "hooks/emit-tool-activity.py").read_text())
            r = subprocess.run(
                [sys.executable, str(hook)],
                input='{"tool_name": "Bash", "tool_input": {}}',
                capture_output=True, text=True, cwd=td)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(
                [p.name for p in Path(td).rglob("activity-feed.jsonl")], [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
