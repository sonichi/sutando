"""Guard: `test:py` must run EVERY discovered file, even after one fails.

The runner used to be `... | while read f; do ... python3 "$f" || exit 1; done`. The
`|| exit 1` exits the pipeline subshell mid-loop, so a failure at sort position 50 of
421 ended the run and 371 files never executed — while the exit code (1) and the
output shape were indistinguishable from a complete run that had one failure. The
"green-but-partial" failure mode, and the reason local runs were trusted as coverage.

A string assertion cannot pin this: the contract is behavioural, and the next edit
that reintroduces an in-loop `exit` would still match any pattern we grepped for. So
these tests EXECUTE the shipped `scripts["test:py"]` from package.json against
synthetic files in a temp tree, and assert the four properties that matter:

  1. an early failure does not prevent later files from running
  2. every failing filename is named in the summary
  3. the final exit is nonzero when any file failed
  4. an all-pass run exits zero and emits no summary

POLICY test — sibling of runner-glob.test.py, which pins discovery; this pins
continuation.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PASSING = 'print("ok")\n'
FAILING = 'import sys\nprint("boom")\nsys.exit(1)\n'


class RunnerContinuesAfterFailureTest(unittest.TestCase):
    def _runner(self) -> str:
        pkg = json.loads((REPO / "package.json").read_text())
        script = pkg.get("scripts", {}).get("test:py", "")
        self.assertTrue(script, "package.json must define scripts['test:py']")
        return script

    def _run(self, files: dict[str, str]) -> subprocess.CompletedProcess:
        """Execute the SHIPPED runner in a temp tree holding `files`."""
        with tempfile.TemporaryDirectory() as td:
            tests = Path(td) / "tests"
            tests.mkdir()
            for name, body in files.items():
                (tests / name).write_text(body)
            return subprocess.run(
                ["sh", "-c", self._runner()],
                cwd=td, capture_output=True, text=True, timeout=120,
            )

    def test_early_failure_does_not_stop_later_files(self) -> None:
        r = self._run({"a.test.py": PASSING,
                       "b.test.py": FAILING,
                       "c.test.py": PASSING})
        for name in ("a.test.py", "b.test.py", "c.test.py"):
            self.assertIn(name, r.stdout,
                          f"{name} never ran — the runner stopped early")
        self.assertIn("ok", r.stdout, "a passing file after the failure produced no output")

    def test_exit_is_nonzero_when_any_file_fails(self) -> None:
        r = self._run({"a.test.py": PASSING, "b.test.py": FAILING})
        self.assertNotEqual(r.returncode, 0,
                            "a failing file must still fail the run")

    def test_every_failing_filename_is_summarized(self) -> None:
        r = self._run({"a.test.py": FAILING,
                       "b.test.py": PASSING,
                       "c.test.py": FAILING})
        self.assertIn("a.test.py", r.stdout)
        self.assertIn("c.test.py", r.stdout)
        summary = [l for l in r.stdout.splitlines() if "failed:" in l]
        self.assertTrue(summary, "no summary line naming the failures")
        self.assertIn("a.test.py", summary[-1])
        self.assertIn("c.test.py", summary[-1])
        self.assertNotIn("b.test.py", summary[-1],
                         "a passing file must not appear in the failure summary")

    def test_all_pass_exits_zero_and_emits_no_summary(self) -> None:
        r = self._run({"a.test.py": PASSING, "b.test.py": PASSING})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("failed:", r.stdout,
                         "an all-pass run must not print a failure summary")

    def test_every_discovered_file_is_executed(self) -> None:
        """The magnitude guard: files run must equal files discovered, with the
        failure placed FIRST so a fail-fast runner would skip the rest."""
        files = {"a.test.py": FAILING}
        files.update({f"z{i}.test.py": PASSING for i in range(6)})
        r = self._run(files)
        ran = r.stdout.count("--- ")
        self.assertEqual(ran, len(files),
                         f"ran {ran} of {len(files)} discovered files")

    def test_node_modules_is_excluded(self) -> None:
        """A vendored *.test.py must not be executed by the local runner."""
        with tempfile.TemporaryDirectory() as td:
            tests = Path(td) / "tests"
            (tests / "node_modules" / "pkg").mkdir(parents=True)
            (tests / "a.test.py").write_text(PASSING)
            (tests / "node_modules" / "pkg" / "vendored.test.py").write_text(FAILING)
            r = subprocess.run(["sh", "-c", self._runner()],
                               cwd=td, capture_output=True, text=True, timeout=120)
        self.assertNotIn("vendored.test.py", r.stdout)
        self.assertEqual(r.returncode, 0,
                         "a vendored failing test must not fail the local run")


if __name__ == "__main__":
    unittest.main(verbosity=2)
