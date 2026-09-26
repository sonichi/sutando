#!/usr/bin/env python3
"""The documented submit-use-case entry point asks the duplicate checker itself.

`gh issue create` is spawned by this Python script as a child process, which a
`PreToolUse[Bash]` hook inspecting the outer command never sees. The script
therefore runs the same checker before spawning; a refusal must stop the create.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/submit-use-case/scripts/submit_use_case.py"
TITLE = "Run your weekly report hands-free"


class DuplicateGate(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        # Same layout as the repo, so the script's own checker path resolves.
        sub = self.d / "skills/submit-use-case/scripts"
        sub.mkdir(parents=True)
        shutil.copy(SCRIPT, sub / "submit_use_case.py")
        self.script = sub / "submit_use_case.py"
        self.checker = self.d / "skills/proactive-loop/scripts/gh-duplicate-check.py"
        self.checker.parent.mkdir(parents=True)
        self.calls = self.d / "gh-calls.log"
        bindir = self.d / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(
            "#!/bin/bash\n"
            f"echo \"$*\" >> {self.calls}\n"
            "case \"$*\" in\n"
            "  'issue list'*) echo '[]';;\n"
            "  'issue create'*) echo https://github.com/o/r/issues/1;;\n"
            "esac\n")
        gh.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")

    def stub_checker(self, rc, out=""):
        self.checker.write_text(f"import sys\nprint({out!r})\nsys.exit({rc})\n")

    def run_submit(self):
        return subprocess.run([sys.executable, str(self.script), "--title", TITLE,
                               "--summary", "s", "--issue-only"],
                              capture_output=True, text=True, env=self.env)

    def created(self):
        return "issue create" in (self.calls.read_text() if self.calls.exists() else "")

    def test_a_checker_refusal_prevents_the_issue_create_child(self):
        self.stub_checker(1, "candidate: sonichi/sutando#999 (open)")
        r = self.run_submit()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("duplicate-check: refused", r.stderr)
        self.assertIn("#999", r.stderr, "the checker's reason reaches the caller")
        self.assertIn("issue list", self.calls.read_text(),
                      "the run reached the live probes, so the refusal is the gate's")
        self.assertFalse(self.created(), "gh issue create was spawned past a refusal")

    def test_CONTROL_a_clear_checker_lets_the_create_run(self):
        self.stub_checker(0)
        r = self.run_submit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.created())
        self.assertIn("issue: https://github.com/o/r/issues/1", r.stdout)

    def test_a_checker_that_cannot_answer_warns_and_does_not_enforce(self):
        """Same fail-open as hooks/gh-policy-gate.py; both name it."""
        self.stub_checker(2)
        r = self.run_submit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not enforcing", r.stderr)
        self.assertTrue(self.created())

    def test_the_checker_is_asked_with_the_repo_and_the_title(self):
        self.checker.write_text("import sys, json, pathlib\n"
                                f"pathlib.Path({str(self.d / 'argv.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
                                "sys.exit(0)\n")
        self.run_submit()
        argv = json.loads((self.d / "argv.json").read_text())
        self.assertEqual(argv, ["--repo", "sonichi/sutando", "--title", TITLE])


if __name__ == "__main__":
    unittest.main()
