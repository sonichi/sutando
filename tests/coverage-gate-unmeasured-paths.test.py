#!/usr/bin/env python3
"""Exercise coverage advisory attribution and failure handling through the full gate."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _helpers.hermetic_env import hermetic_env, isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
CHANGED = (
    "src/nested/shared.py",
    "scripts/shared.py",
    "outside/nested/shared.py",
    "scripts/nested/shared.py",
    "tests/omitted.test.py",
)
UNMEASURED = {"outside/nested/shared.py", "scripts/nested/shared.py"}
FRESH_REPORT = {
    "files": {
        "src/nested/shared.py": {"executed_lines": [1], "missing_lines": []},
        "scripts/shared.py": {"executed_lines": [], "missing_lines": [1]},
    }
}
DIFF_REPORT = "### fixture diff coverage\n\nControlled diff-cover report.\n"
JOB_SUMMARY_PREFIX = "Existing job summary.\n"
XML = """<?xml version="1.0" ?>
<coverage><sources><source>src</source><source>scripts</source></sources>
<packages><package><classes>
<class filename="nested/shared.py"/><class filename="shared.py"/>
</classes></package></packages></coverage>
"""

FAKE_PYTHON = r'''
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:2] != ["-m", "coverage"]:
    with Path("helper-calls.jsonl").open("a") as log:
        log.write(json.dumps(args) + "\n")
    os.execv(sys.executable, [sys.executable] + args)

args = args[2:]
with Path("coverage-calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("Coverage.py fixture")
elif args[0] == "report":
    print("91" if "--format=total" in args else "TOTAL 10 1 91%")
elif args[0] == "json":
    controls = json.loads(Path("fixture-controls.json").read_text())
    if controls["json_rc"]:
        print("fixture coverage JSON generation failed", file=sys.stderr)
        sys.exit(controls["json_rc"])
    output = "coverage.json"
    for index, arg in enumerate(args):
        if arg in ("-o", "--output"):
            output = args[index + 1]
        elif arg.startswith("--output="):
            output = arg.split("=", 1)[1]
    Path(output).write_text(Path("fresh-coverage-payload.json").read_text())
else:
    raise SystemExit("Unexpected coverage command: " + repr(args))
'''

FAKE_DIFF_COVER = r'''
import json
import sys
from pathlib import Path

args = sys.argv[1:]
Path("diff-cover-args.json").write_text(json.dumps(args))
output = args[args.index("--markdown-report") + 1]
Path(output).write_text("### fixture diff coverage\n\nControlled diff-cover report.\n")
sys.exit(json.loads(Path("fixture-controls.json").read_text())["diff_cover_rc"])
'''


class FullGateUnmeasuredPaths(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="coverage-gate-paths-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.bin = root / "bin"
        self.home = root / "home"
        for path in (self.repo, self.bin, self.home):
            path.mkdir()
        self.job_summary = root / "job-summary.md"
        self.job_summary.write_text(JOB_SUMMARY_PREFIX)
        self.env = hermetic_env(
            home=self.home,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            BASE_REF="base",
            COVERAGE_GATE_PRECOMPUTED="1",
            COVERAGE_GATE_FAIL_UNDER="95",
            GITHUB_STEP_SUMMARY=str(self.job_summary),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME="coverage fixture",
            GIT_AUTHOR_EMAIL="coverage@example.test",
            GIT_COMMITTER_NAME="coverage fixture",
            GIT_COMMITTER_EMAIL="coverage@example.test",
        )
        self._git("init", "-q", "-b", "base")
        (self.repo / "scripts").mkdir()
        for name in ("coverage-gate.sh", "coverage_unmeasured.py"):
            shutil.copy2(REPO / "scripts" / name, self.repo / "scripts" / name)
        (self.repo / ".coveragerc").write_text(
            "[run]\nsource =\n    src\n    scripts\nomit =\n    tests/*\n"
        )
        self._git("add", "-A")
        self._git("commit", "-qm", "base fixture")
        self._git("switch", "-qc", "topic")
        for name in CHANGED:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("value = 1\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "changed Python fixture")
        (self.repo / ".coverage").write_text("precomputed coverage fixture\n")
        (self.repo / "coverage.xml").write_text(XML)
        (self.repo / "fresh-coverage-payload.json").write_text(json.dumps(FRESH_REPORT))
        interpreter = self.bin / "fixture python"
        interpreter.symlink_to(sys.executable)
        for name, source in (("python3", FAKE_PYTHON), ("diff-cover", FAKE_DIFF_COVER)):
            script = self.bin / (name + ".py")
            script.write_text(source.lstrip())
            path = self.bin / name
            path.write_text(
                "#!/bin/sh\nexec " + shlex.quote(str(interpreter)) + " "
                + shlex.quote(str(script)) + ' "$@"\n'
            )
            path.chmod(0o755)

    def _git(self, *args):
        subprocess.run(
            ["git", *args], cwd=self.repo, env=self.env,
            check=True, capture_output=True, text=True, timeout=30,
        )

    def _run_gate(self, *, json_rc=0, diff_cover_rc=0):
        (self.repo / "fixture-controls.json").write_text(json.dumps({
            "json_rc": json_rc, "diff_cover_rc": diff_cover_rc,
        }))
        return subprocess.run(
            ["bash", "scripts/coverage-gate.sh"], cwd=self.repo, env=self.env,
            capture_output=True, text=True, timeout=30,
        )

    def _calls(self, name):
        path = self.repo / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def _assert_summary(self, result, status):
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, status, output)
        summary = (self.repo / "coverage-summary.md").read_text()
        verdict = "Diff coverage PASSES" if status == 0 else "Diff coverage BELOW"
        self.assertIn(verdict, summary)
        self.assertIn(DIFF_REPORT, summary)
        self.assertEqual(self.job_summary.read_text(), JOB_SUMMARY_PREFIX + summary)
        self.assertEqual(json.loads((self.repo / "diff-cover-args.json").read_text())[0],
                         "coverage.xml")
        return output, summary

    def _assert_unmeasured_paths(self, output, summary):
        listed = set(re.findall(r"(?m)^-\s+`([^`]+)`\s*$", summary))
        self.assertEqual(listed, UNMEASURED, summary)
        for path in CHANGED:
            if path in UNMEASURED:
                self.assertIn(path, output)
            else:
                self.assertNotIn(path, output)
                self.assertNotIn(path, summary)

    def test_exact_json_paths_distinguish_nested_same_name_files_and_zero_hits(self):
        output, summary = self._assert_summary(self._run_gate(), 0)
        self._assert_unmeasured_paths(output, summary)
        self.assertEqual((self.repo / "coverage.xml").read_text(), XML)

    def test_json_is_regenerated_instead_of_reading_a_stale_report(self):
        (self.repo / "coverage.json").write_text(json.dumps({
            "files": {path: {"executed_lines": [1]} for path in CHANGED},
        }))
        output, summary = self._assert_summary(self._run_gate(), 0)
        self._assert_unmeasured_paths(output, summary)
        self.assertEqual(json.loads((self.repo / "coverage.json").read_text()), FRESH_REPORT)
        self.assertEqual(sum(args[0] == "json" for args in self._calls("coverage-calls.jsonl")), 1)
        self.assertEqual(len(self._calls("helper-calls.jsonl")), 1)

    def _assert_literal_paths_preserve_verdict(self, status):
        paths = {
            "outside/a file.py",
            "outside/ leading and trailing .py",
            "outside/star*.py",
            "outside/query?.py",
            "outside/[ab].py",
            "outside/$cash;quote'.py",
        }
        for name in paths:
            (self.repo / name).write_text("value = 1\n")
        self._git("add", "outside")
        self._git("commit", "-qm", "literal filename fixture")
        for name in ("outside/star-match.py", "outside/query1.py", "outside/a.py"):
            (self.repo / name).write_text("untracked = 1\n")
        output, summary = self._assert_summary(self._run_gate(diff_cover_rc=status), status)
        expected = sorted(UNMEASURED | paths)
        self.assertEqual([line[2:] for line in output.splitlines() if line.startswith("  ")],
                         expected, output)
        self.assertEqual(re.findall(r"(?m)^-\s+`([^`]+)`$", summary), expected, summary)

    def test_literal_paths_preserve_a_passing_gate(self):
        self._assert_literal_paths_preserve_verdict(0)

    def test_literal_paths_preserve_a_failing_gate(self):
        self._assert_literal_paths_preserve_verdict(7)

    def _assert_json_failure_preserves_verdict(self, status):
        (self.repo / "coverage.json").write_text(json.dumps({"files": {}}))
        result = self._run_gate(json_rc=23, diff_cover_rc=status)
        output, summary = self._assert_summary(result, status)
        self.assertIn("unavailable", output.lower())
        self.assertNotIn("### ⚠️ Not measured", summary)
        for path in CHANGED:
            self.assertNotIn(path, output)
            self.assertNotIn(path, summary)
        self.assertEqual(self._calls("helper-calls.jsonl"), [])
        self.assertEqual(sum(args[0] == "json" for args in self._calls("coverage-calls.jsonl")), 1)

    def test_json_generation_failure_with_stale_report_preserves_a_passing_gate(self):
        self._assert_json_failure_preserves_verdict(0)

    def test_json_generation_failure_with_stale_report_preserves_a_failing_gate(self):
        self._assert_json_failure_preserves_verdict(7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
