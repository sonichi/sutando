"""Guard: `test:py` must run EVERY discovered file, and must never report a green
run it did not measure — early failure, zero discovery, or a failed `find`."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PASSING = 'print("ok")\n'
STDIN_EATER = 'import sys\nsys.stdin.read()\nprint("ate stdin")\n'
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

    def test_all_pass_exits_zero_with_a_total_and_no_failure_summary(self) -> None:
        r = self._run({"a.test.py": PASSING, "b.test.py": PASSING})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("failed:", r.stdout,
                         "an all-pass run must not print a failure summary")
        self.assertIn("2 file(s)", r.stdout,
                      "an all-pass run must report HOW MANY files ran — a bare exit 0 "
                      "cannot be told apart from a run that discovered nothing")

    def test_zero_discovery_fails_instead_of_reporting_success(self) -> None:
        """No files is not success: the loop body never runs, so `fail` stays 0."""
        r = self._run({})
        self.assertNotEqual(r.returncode, 0,
                            "a run that discovered zero test files exited 0 — "
                            "'ran nothing' must not report as 'all passed'")
        self.assertIn("0 test files discovered", r.stdout,
                      "the zero case must say so, not just fail")

    def test_zero_discovery_is_distinguishable_from_a_real_failure(self) -> None:
        """Both exit nonzero, so the exit code alone cannot separate them."""
        empty = self._run({})
        real = self._run({"a.test.py": FAILING})
        self.assertNotEqual(empty.returncode, 0)
        self.assertNotEqual(real.returncode, 0)
        self.assertNotIn("failed:", empty.stdout,
                         "the empty case must not claim a file failed")
        self.assertNotIn("0 test files discovered", real.stdout,
                         "a real failure must not be reported as empty discovery")

    def test_every_discovered_file_is_executed(self) -> None:
        """Files run must equal files discovered; the failure is FIRST so a fail-fast
        runner would visibly skip the rest."""
        files = {"a.test.py": FAILING}
        files.update({f"z{i}.test.py": PASSING for i in range(6)})
        r = self._run(files)
        ran = r.stdout.count("--- ")
        self.assertEqual(ran, len(files),
                         f"ran {ran} of {len(files)} discovered files")

    def test_partial_discovery_is_not_reported_as_a_clean_run(self) -> None:
        """A `find` that prints some paths then exits nonzero (unreadable subdir) must
        not yield a green run — a false-complete result carrying a reassuring count."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "tests").mkdir()
            (Path(td) / "tests" / "a.test.py").write_text(PASSING)
            bin_ = Path(td) / "bin"
            bin_.mkdir()
            stub = bin_ / "find"
            stub.write_text('#!/bin/sh\n'
                            'printf "%s\\n" tests/a.test.py\n'
                            'echo "find: tests/locked: Permission denied" >&2\n'
                            'exit 1\n')
            stub.chmod(0o755)
            r = subprocess.run(
                ["sh", "-c", self._runner()], cwd=td,
                env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"},
                capture_output=True, text=True, timeout=120,
            )
        self.assertNotEqual(r.returncode, 0,
                            "discovery exited nonzero but the run reported success")
        self.assertNotIn("all passed", r.stdout,
                         "a partial file list must not be summarised as all passed")

    def test_a_test_reading_stdin_does_not_swallow_the_remaining_files(self) -> None:
        """A child inheriting the pipe's stdin can consume the queued filenames, ending
        the loop early while it still prints a summary — a green run of a partial list."""
        files = {"a.test.py": STDIN_EATER}
        files.update({f"z{i}.test.py": PASSING for i in range(4)})
        r = self._run(files)
        ran = r.stdout.count("--- ")
        self.assertEqual(ran, len(files),
                         f"ran {ran} of {len(files)} — a test consumed the pending queue")
        self.assertIn(f"{len(files)} file(s)", r.stdout,
                      "the summary must report the full count, not the truncated one")

    def test_partial_sort_is_not_reported_as_a_clean_run(self) -> None:
        """A `sort` that prints some paths then exits nonzero must not yield a green run —
        the same false-complete hole as partial discovery, one pipeline stage later."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "tests").mkdir()
            for i in range(3):
                (Path(td) / "tests" / f"z{i}.test.py").write_text(PASSING)
            bin_ = Path(td) / "bin"
            bin_.mkdir()
            stub = bin_ / "sort"
            stub.write_text('#!/bin/sh\n'
                            'printf "%s\\n" tests/z0.test.py\n'
                            'echo "sort: write failed" >&2\n'
                            'exit 1\n')
            stub.chmod(0o755)
            r = subprocess.run(
                ["sh", "-c", self._runner()], cwd=td,
                env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"},
                capture_output=True, text=True, timeout=120,
            )
        self.assertNotEqual(r.returncode, 0,
                            "sort exited nonzero but the run reported success")
        self.assertNotIn("all passed", r.stdout,
                         "a partial ordering must not be summarised as all passed")

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
