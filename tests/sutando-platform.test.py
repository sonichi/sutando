#!/usr/bin/env python3
"""Unit coverage for every OS backend in src/sutando_platform.py."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import sutando_platform as platform  # noqa: E402


def completed(returncode=0, stdout=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class PlatformBackends(unittest.TestCase):
    def platform(self, *, mac=False, windows=False, linux=False):
        return (
            patch.object(platform, "is_macos", return_value=mac),
            patch.object(platform, "is_windows", return_value=windows),
            patch.object(platform, "is_linux", return_value=linux),
        )

    def test_platform_predicates(self):
        for value, expected in (
            ("win32", (True, False, False)),
            ("darwin", (False, True, False)),
            ("linux", (False, False, True)),
        ):
            with patch.object(platform.sys, "platform", value):
                self.assertEqual(
                    (platform.is_windows(), platform.is_macos(), platform.is_linux()),
                    expected,
                )

    def test_notify_backends_and_failures(self):
        run = Mock(return_value=completed())
        mac, win, linux = self.platform(mac=True)
        with mac, win, linux, patch.object(platform.subprocess, "run", run):
            platform.notify('say "hi"', 'title "x"')
        self.assertEqual(run.call_args.args[0][0], "osascript")
        self.assertIn('\\"hi\\"', run.call_args.args[0][-1])

        run.reset_mock()
        mac, win, linux = self.platform(windows=True)
        with mac, win, linux, patch.object(platform.subprocess, "run", run):
            platform.notify("it's ready", "owner's agent")
        self.assertEqual(run.call_args.args[0][0], "powershell.exe")
        self.assertIn("it''s ready", run.call_args.args[0][-1])

        mac, win, linux = self.platform(linux=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run", side_effect=FileNotFoundError):
            platform.notify("message")
        mac, win, linux = self.platform(mac=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run", side_effect=RuntimeError):
            platform.notify("message")

    def test_clipboard_backends_and_failures(self):
        cases = (
            (dict(mac=True), "pbpaste"),
            (dict(windows=True), "powershell.exe"),
            (dict(linux=True), "xclip"),
        )
        for flags, executable in cases:
            with self.subTest(executable=executable):
                mac, win, linux = self.platform(**flags)
                with mac, win, linux, patch.object(
                        platform.subprocess, "run",
                        return_value=completed(stdout="copied")) as run:
                    self.assertEqual(platform.clipboard_read(), "copied")
                    platform.clipboard_write("text")
                self.assertEqual(run.call_args_list[0].args[0][0], executable)
        mac, win, linux = self.platform(mac=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run", side_effect=RuntimeError):
            self.assertEqual(platform.clipboard_read(), "")
            platform.clipboard_write("text")

    def test_process_executable_backends(self):
        mac, win, linux = self.platform(windows=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run",
                return_value=completed(stdout="C:\\Python\\python.exe\n")) as run:
            self.assertEqual(platform.process_executable("42"), "C:\\Python\\python.exe")
            self.assertIn("ProcessId = 42", run.call_args.args[0][-1])
            self.assertIsNone(platform.process_executable(-1))

        mac, win, linux = self.platform(linux=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run", return_value=completed(stdout="/usr/bin/python\n")):
            self.assertEqual(platform.process_executable(42), "/usr/bin/python")
        with mac, win, linux, patch.object(
                platform.subprocess, "run", return_value=completed(returncode=1)):
            self.assertIsNone(platform.process_executable(42))

        mac, win, linux = self.platform()
        with mac, win, linux:
            self.assertIsNone(platform.process_executable(42))
        mac, win, linux = self.platform(windows=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run", side_effect=RuntimeError):
            self.assertIsNone(platform.process_executable(42))

    def test_process_snapshot_backends(self):
        mac, win, linux = self.platform(linux=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run", return_value=completed(stdout="PID PPID ARGS\n")):
            self.assertEqual(platform.process_snapshot(), "PID PPID ARGS\n")
        with mac, win, linux, patch.object(
                platform.subprocess, "run", return_value=completed(returncode=1)):
            self.assertIsNone(platform.process_snapshot())

        payload = (
            '[{"ProcessId":9,"ParentProcessId":1,"CommandLine":"python\\nworker.py"},'
            '{"ProcessId":2,"ParentProcessId":0,"CommandLine":"cmd"},'
            '{"ProcessId":3,"CommandLine":""},42]'
        )
        mac, win, linux = self.platform(windows=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run", return_value=completed(stdout="\ufeff" + payload)):
            self.assertEqual(
                platform.process_snapshot(),
                "PID PPID ARGS\n2 0 cmd\n9 1 python worker.py\n",
            )
        for result in (completed(returncode=1), completed(stdout=""), completed(stdout="{")):
            with mac, win, linux, patch.object(platform.subprocess, "run", return_value=result):
                self.assertIsNone(platform.process_snapshot())
        mac, win, linux = self.platform()
        with mac, win, linux:
            self.assertIsNone(platform.process_snapshot())

    def test_probe_pids_backends(self):
        mac, win, linux = self.platform(linux=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run", return_value=completed(stdout="1\n2\n")):
            self.assertEqual(platform.probe_pids("worker"), (["1", "2"], True))
        with mac, win, linux, patch.object(
                platform.subprocess, "run", return_value=completed(returncode=2)):
            self.assertEqual(platform.probe_pids("worker"), ([], False))

        mac, win, linux = self.platform(windows=True)
        for pattern, fragment in (
            ("both", ".Contains('both')"),
            ("^start", ".StartsWith('start')"),
            ("end$", ".EndsWith('end')"),
            ("^exact$", "$cl -eq 'exact'"),
        ):
            with self.subTest(pattern=pattern), mac, win, linux, patch.object(
                    platform.subprocess, "run",
                    return_value=completed(stdout="7\nnot-a-pid\n8\n")) as run:
                self.assertEqual(platform.probe_pids(pattern), (["7", "8"], True))
                self.assertIn(fragment, run.call_args.args[0][-1])
        with mac, win, linux, patch.object(
                platform.subprocess, "run", return_value=completed(returncode=1)):
            self.assertEqual(platform.probe_pids("worker"), ([], False))
        with mac, win, linux, patch.object(
                platform.subprocess, "run", side_effect=RuntimeError):
            self.assertEqual(platform.probe_pids("worker"), ([], False))
        mac, win, linux = self.platform()
        with mac, win, linux:
            self.assertEqual(platform.probe_pids("worker"), ([], False))
            self.assertEqual(platform.find_pids("worker"), [])

    def test_process_running_and_kill(self):
        mac, win, linux = self.platform(linux=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run", return_value=completed()) as run:
            self.assertTrue(platform.is_process_running("worker"))
            platform.kill_process("worker")
        self.assertEqual(run.call_args.args[0][0], "pkill")

        mac, win, linux = self.platform(windows=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run",
                return_value=completed(stdout="ProcessId\n42\n")) as run:
            self.assertTrue(platform.is_process_running("it's"))
            platform.kill_process("it's")
        self.assertIn("it''s", run.call_args.args[0][-1])

        mac, win, linux = self.platform()
        with mac, win, linux:
            self.assertFalse(platform.is_process_running("worker"))
            platform.kill_process("worker")
        mac, win, linux = self.platform(windows=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run", side_effect=RuntimeError):
            self.assertFalse(platform.is_process_running("worker"))
            platform.kill_process("worker")

    def test_port_probe(self):
        mac, win, linux = self.platform(linux=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run", return_value=completed()):
            self.assertTrue(platform.is_port_in_use(7845))
        mac, win, linux = self.platform(windows=True)
        with mac, win, linux, patch.object(
                platform.subprocess, "run",
                return_value=completed(stdout=" TCP  0.0.0.0:7845  0.0.0.0:0  LISTENING  42\n")):
            self.assertTrue(platform.is_port_in_use(7845))
        with mac, win, linux, patch.object(
                platform.subprocess, "run", return_value=completed(stdout="")):
            self.assertFalse(platform.is_port_in_use(7845))
        with mac, win, linux, patch.object(
                platform.subprocess, "run", side_effect=RuntimeError):
            self.assertFalse(platform.is_port_in_use(7845))

    def test_capture_screen_backends(self):
        with tempfile.TemporaryDirectory() as td:
            target = str(Path(td) / "nested" / "shot.jpg")
            mac, win, linux = self.platform(mac=True)
            with mac, win, linux, patch.object(
                    platform.subprocess, "run", return_value=completed()), \
                    patch.object(platform.Path, "exists", return_value=True):
                self.assertTrue(platform.capture_screen(target, "jpeg"))

            mac, win, linux = self.platform(windows=True)
            with mac, win, linux, patch.object(
                    platform.subprocess, "run", return_value=completed()) as run, \
                    patch.object(platform.Path, "exists", return_value=True):
                self.assertTrue(platform.capture_screen(target, "png"))
                self.assertIn("ImageFormat]::Png", run.call_args.args[0][-1])

            mac, win, linux = self.platform()
            with mac, win, linux:
                self.assertFalse(platform.capture_screen(target))
            mac, win, linux = self.platform(mac=True)
            with mac, win, linux, patch.object(
                    platform.Path, "mkdir", side_effect=OSError), \
                    patch.object(platform.subprocess, "run", side_effect=RuntimeError):
                self.assertFalse(platform.capture_screen(target))

    def test_open_with_default_backends(self):
        mac, win, linux = self.platform(mac=True)
        with mac, win, linux, patch.object(platform.subprocess, "run") as run:
            platform.open_with_default("target")
            self.assertEqual(run.call_args.args[0], ["open", "target"])

        mac, win, linux = self.platform(windows=True)
        with mac, win, linux, patch.object(platform.os, "startfile", create=True) as start:
            platform.open_with_default("target")
            start.assert_called_once_with("target")

        mac, win, linux = self.platform(linux=True)
        with mac, win, linux, patch.object(platform.subprocess, "run") as run:
            platform.open_with_default("target")
            self.assertEqual(run.call_args.args[0], ["xdg-open", "target"])
        with mac, win, linux, patch.object(
                platform.subprocess, "run", side_effect=RuntimeError):
            platform.open_with_default("target")


if __name__ == "__main__":
    unittest.main()
