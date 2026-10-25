#!/usr/bin/env python3
"""Hermetic checks for copyable Bash/Zsh worker attach guidance."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))
import spawn_worker as sw


def shell_argv(shell, command):
    flags = ["--noprofile", "--norc"] if shell == "bash" else ["-f"]
    return [shell, *flags, "-c", command]


class TestShellArguments(unittest.TestCase):
    def test_socket_and_exact_target_are_literal_arguments_in_both_shells(self):
        pairs = [
            ("/tmp/plain.sock", "sutando-worker-" + "a" * 32),
            ("/tmp/socket with spaces", "worker"),
            ("=relative-socket", "worker"),
            ("/tmp/line\nbreak", "line\nbreak"),
            ("-S", "-t"),
            ("", ""),
            ("/tmp/日本語", "équipe"),
            ("/tmp/quotes'\" $HOME $(printf BAD) `printf BAD`;|&<>*?[!]~\\.sock",
             "worker'\" $HOME $(printf BAD) `printf BAD`;|&<>*?[!]~\\"),
        ]
        for shell in ("bash", "zsh"):
            with self.subTest(shell=shell):
                if shell == "zsh" and shutil.which(shell) is None:
                    self.skipTest("zsh is not installed")
                self.assertIsNotNone(shutil.which(shell), shell)
                self.assert_shell_arguments(shell, pairs)

    def assert_shell_arguments(self, shell, pairs):
        for socket, session in pairs:
            with self.subTest(shell=shell, socket=socket):
                command = sw.attach_command(socket, session)
                script = "tmux() { printf '%s\\0' \"$@\"; }; " + command
                r = subprocess.run(shell_argv(shell, script),
                                   capture_output=True, timeout=5)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.split(b"\0")[:-1], [
                    b"-S", os.fsencode(socket), b"attach-session", b"-t",
                    os.fsencode("=" + session)])


class TestAttachDelegation(unittest.TestCase):
    def test_command_quotes_the_shared_argv(self):
        with mock.patch.object(sw, "attach_argv", return_value=["tmux", "=worker's name"]) as argv:
            self.assertEqual(sw.attach_command("raw server", "raw session"),
                             "'tmux' '=worker'\"'\"'s name'")
        argv.assert_called_once_with("raw server", "raw session")


if __name__ == "__main__":
    unittest.main(verbosity=2)
