#!/usr/bin/env python3
"""Shared dispatcher execution: no shell, no failed output, no residual cwd."""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from policy import sandbox_runner as sr  # noqa: E402


class Answers(unittest.TestCase):
    def setUp(self):
        self.cwd = None
        self.runner = sr.RUNNERS["claude"]
        self.patches = [mock.patch.object(sr, "select_runner", return_value=(self.runner, "claude.exe")),
                        mock.patch.object(sr, "native_binary", side_effect=lambda path: path)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def capture(self, argv, **kwargs):
        self.cwd = kwargs["cwd"]
        self.assertEqual(list(Path(self.cwd).iterdir()), [])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        for flag in ("--restricted", "--safe-mode", "--strict-mcp-config",
                     "--disable-slash-commands", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[-2:], ["--", sr.context_preamble() + self.question])
        return subprocess.CompletedProcess(argv, 0, "Answer café\nsecond line\n", "")

    def test_answer_preserves_untrusted_prompt_and_cleans_cwd(self):
        self.question = '" & echo PWN | %PATH%\n--dangerously-skip-permissions café'
        with mock.patch.object(sr.subprocess, "run", side_effect=self.capture):
            self.assertEqual(sr.answer(self.question), "Answer café\nsecond line\n")
        self.assertFalse(Path(self.cwd).exists())

    def test_failed_output_is_never_returned(self):
        self.question = "q"
        for result in (subprocess.CompletedProcess([], 1, "SECRET", "SECRET"),
                       subprocess.CompletedProcess([], 0, " \n", "SECRET"),
                       OSError("SECRET"), ValueError("SECRET"),
                       subprocess.TimeoutExpired("SECRET", 1, output="SECRET")):
            with self.subTest(result=type(result).__name__):
                def run(argv, **kwargs):
                    self.capture(argv, **kwargs)
                    if isinstance(result, Exception):
                        raise result
                    return result
                with mock.patch.object(sr.subprocess, "run", side_effect=run):
                    with self.assertRaises(sr.SandboxUnavailable) as failure:
                        sr.answer(self.question, timeout=1)
                self.assertEqual(str(failure.exception), sr.UNAVAILABLE)
                self.assertFalse(Path(self.cwd).exists())

    def test_missing_safe_runner_never_spawns(self):
        with mock.patch.object(sr, "select_runner", return_value=None), \
                mock.patch.object(sr.subprocess, "run") as run:
            with self.assertRaises(sr.SandboxUnavailable):
                sr.answer("q")
        run.assert_not_called()

    def test_cli_failure_is_generic_and_utf8(self):
        stdin = mock.Mock(buffer=io.BytesIO("café".encode("utf-8")))
        stdout = mock.Mock(buffer=io.BytesIO())
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sr, "answer", side_effect=sr.SandboxUnavailable("SECRET")) as answer:
            self.assertEqual(sr.main(), 1)
        answer.assert_called_once_with("café")
        self.assertEqual(stdout.buffer.getvalue().decode("utf-8"), sr.UNAVAILABLE)

    def test_cli_success_and_invalid_encoding(self):
        for data, expected, status in ((b"question", "café\n", 0), (b"\xff", sr.UNAVAILABLE, 1)):
            with self.subTest(data=data):
                stdin = mock.Mock(buffer=io.BytesIO(data))
                stdout = mock.Mock(buffer=io.BytesIO())
                with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout), \
                        mock.patch.object(sr, "answer", return_value="café\n") as answer:
                    self.assertEqual(sr.main(), status)
                self.assertEqual(stdout.buffer.getvalue().decode("utf-8"), expected)
                if status:
                    answer.assert_not_called()


class WindowsBinaries(unittest.TestCase):
    def test_npm_native_install_and_unsupported_shims(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(sys, "platform", "win32"):
            shim = Path(root) / "claude.cmd"
            with self.assertRaises(sr.SandboxUnavailable):
                sr.native_binary(str(shim))
            native = Path(root) / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
            native.parent.mkdir(parents=True)
            native.touch()
            self.assertEqual(sr.native_binary(str(shim)), str(native))
            self.assertEqual(sr.native_binary(str(shim.with_suffix(".ps1"))), str(native))
            self.assertEqual(sr.native_binary(str(native)), str(native))
            with self.assertRaises(sr.SandboxUnavailable):
                sr.native_binary(str(Path(root) / "untrusted.bat"))

    def test_posix_binary_is_unchanged(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(sr.native_binary("/native/claude"), "/native/claude")


if __name__ == "__main__":
    unittest.main()
