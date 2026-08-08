#!/usr/bin/env python3
"""The tool-activity PostToolUse hook must be TERSE and never leak secrets.

The hook feeds the SCP `/verbose` activity stream (kind:"tool"). Its safety
contract: a Bash command's args can carry secrets (`export TOKEN=…`,
`curl -H "Authorization: …"`), so the hook emits the verb only (plus a safe
subcommand for verb-based tools) — never the args. Everything else is a short,
non-sensitive locator. And it is fail-open: a hook must never crash a tool call.

Run: python3 tests/hooks-emit-tool-activity.test.py
"""
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "emit_tool_activity", REPO / "hooks" / "emit-tool-activity.py")
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


class TargetNoLeakTests(unittest.TestCase):
    def test_bash_shows_verb_only_never_secret_args(self):
        cases = [
            ({"command": "export TOKEN=supersecret123"}, "export", "supersecret123"),
            ({"command": "curl -H Authorization:Bearer_xyz https://x"}, "curl", "Bearer_xyz"),
            ({"command": "echo hunter2 | base64"}, "echo", "hunter2"),
        ]
        for ti, expect, secret in cases:
            got = hook._target("Bash", ti)
            self.assertEqual(got, expect)
            self.assertNotIn(secret, got)

    def test_bash_verb_plus_safe_subcommand(self):
        self.assertEqual(hook._target("Bash", {"command": "git commit -m msg"}), "git commit")
        self.assertEqual(hook._target("Bash", {"command": "gh pr view 5"}), "gh pr")
        # a subcommand token that looks like an assignment is dropped (no leak)
        self.assertEqual(hook._target("Bash", {"command": "make FOO=secret"}), "make")

    def test_bash_skips_leading_cd_prefix(self):
        # `cd <dir> && <realcmd>` should surface the REAL command, not "cd".
        self.assertEqual(hook._target("Bash", {"command": "cd /a/b && git commit -m x"}), "git commit")
        self.assertEqual(hook._target("Bash", {"command": "cd /repo && python3 run.py"}), "python3 run.py")
        # a bare cd (no &&) still shows cd
        self.assertEqual(hook._target("Bash", {"command": "cd /somewhere"}), "cd")
        # newline-multiline: skip leading cd line, surface the real command
        self.assertEqual(hook._target("Bash", {"command": "cd /repo\ngit push origin"}), "git push")
        self.assertEqual(hook._target("Bash", {"command": "cd /a/b\npython3 x.py\ngit add ."}), "python3 x.py")

    def test_file_and_pattern_tools_are_terse(self):
        self.assertEqual(hook._target("Edit", {"file_path": "/a/b/server.py"}), "server.py")
        self.assertEqual(hook._target("Read", {"file_path": "/x/tasks_view.py"}), "tasks_view.py")
        self.assertEqual(hook._target("Grep", {"pattern": "needle"}), "needle")

    def test_unknown_tool_and_missing_fields_dont_crash(self):
        self.assertEqual(hook._target("MysteryTool", {}), "")
        self.assertEqual(hook._target("Bash", {}), "")


class MainWritePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_ws = hook._workspace
        self._orig_stdin = sys.stdin
        hook._workspace = lambda: Path(self.tmp.name)

    def tearDown(self):
        hook._workspace = self._orig_ws
        sys.stdin = self._orig_stdin
        self.tmp.cleanup()

    def _run(self, payload_text):
        sys.stdin = io.StringIO(payload_text)
        return hook.main()

    def test_writes_terse_line_no_secret_on_real_path(self):
        rc = self._run(json.dumps({"tool_name": "Bash",
                                   "tool_input": {"command": "export API_KEY=leakme"}}))
        self.assertEqual(rc, 0)
        feed = Path(self.tmp.name) / "state" / "activity-feed.jsonl"
        content = feed.read_text()
        rec = json.loads(content.splitlines()[-1])
        self.assertEqual(rec["kind"], "tool")
        self.assertEqual(rec["step"], "Bash: export")
        self.assertNotIn("leakme", content)

    def test_fail_open_on_malformed_stdin(self):
        # A broken hook payload must return 0 and write nothing — never block the tool.
        rc = self._run("this is not json")
        self.assertEqual(rc, 0)
        self.assertFalse((Path(self.tmp.name) / "state" / "activity-feed.jsonl").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
