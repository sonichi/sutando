#!/usr/bin/env python3
"""Contract tests for src/optional_script.py's run_optional_script, the shared fail-open runner.

The Discord, Slack and Telegram bridges call it with a script and args only, and must run exactly
the call they always did. The gateway bridge's build-thread gate also passes `input=`, `env=` and
`isolated=True`. Covered, with real child processes:
  1. a caller that names none of the three gets the unchanged subprocess call;
  2. `input` reaches the script's stdin;
  3. `env` replaces the whole environment: a parent variable outside it is absent;
  4. `isolated` runs python -I;
  5. a missing script, a non-zero exit, empty stdout and a timeout all return None, and only the
     timeout (an execution failure) reaches on_error.

Run: python3 tests/optional-script-runner.test.py   (exit 0 pass / 1 fail)
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from optional_script import run_optional_script  # noqa: E402


class Runner(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = Path(self.td.name)

    def script(self, name, body):
        path = self.dir / name
        path.write_text(body)
        return path

    def test_a_plain_caller_runs_the_unchanged_call(self):
        result = MagicMock(returncode=0, stdout="ok\n")
        with patch.object(Path, "exists", return_value=True), \
             patch("optional_script.subprocess.run", return_value=result) as run:
            self.assertEqual(run_optional_script(Path("/tmp/x.py"), ["a"], timeout=5), "ok")
        run.assert_called_once_with([sys.executable, "/tmp/x.py", "a"], capture_output=True, text=True, timeout=5)

    def test_input_reaches_stdin(self):
        s = self.script("echo.py", "import sys; print(sys.stdin.read().upper())\n")
        self.assertEqual(run_optional_script(s, [], timeout=10, input="question"), "QUESTION")

    def test_env_replaces_the_whole_environment(self):
        s = self.script("env.py", "import os; print(os.environ.get('KEPT', '-'), os.environ.get('SECRET_X', '-'))\n")
        with patch.dict(os.environ, {"SECRET_X": "token"}):
            self.assertEqual(run_optional_script(s, [], timeout=10, env={"KEPT": "yes"}), "yes -")
            self.assertEqual(run_optional_script(s, [], timeout=10), "- token", "without env= it inherits")

    def test_isolated_runs_python_dash_I(self):
        s = self.script("iso.py", "import sys; print(sys.flags.isolated)\n")
        self.assertEqual(run_optional_script(s, [], timeout=10, isolated=True), "1")
        self.assertEqual(run_optional_script(s, [], timeout=10), "0")

    def test_every_failure_returns_none_and_only_execution_failures_are_reported(self):
        errors = []
        self.assertIsNone(run_optional_script(self.dir / "missing.py", [], timeout=10, on_error=errors.append))
        self.assertIsNone(run_optional_script(self.script("exit3.py", "raise SystemExit(3)\n"), [], timeout=10,
                                              on_error=errors.append))
        self.assertIsNone(run_optional_script(self.script("empty.py", "pass\n"), [], timeout=10, on_error=errors.append))
        self.assertEqual(errors, [], "missing, non-zero and empty stay silent")
        self.assertIsNone(run_optional_script(self.script("slow.py", "import time; time.sleep(30)\n"), [],
                                              timeout=0.5, on_error=errors.append))
        self.assertEqual([type(e).__name__ for e in errors], ["TimeoutExpired"])


if __name__ == "__main__":
    unittest.main()
