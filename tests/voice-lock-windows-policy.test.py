#!/usr/bin/env python3
"""Cross-platform unit coverage for voice-lock's Windows-only policy."""

from __future__ import annotations

import ctypes
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "voice_lock_windows_policy", REPO / "scripts" / "voice-lock.py")
voice_lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(voice_lock)


class WindowsVoiceLockPolicy(unittest.TestCase):
    def test_pid_alive_windows_outcomes(self):
        api = Mock()
        api.OpenProcess.return_value = None
        with patch.object(voice_lock.os, "name", "nt"), \
                patch.object(ctypes, "WinDLL", return_value=api, create=True), \
                patch.object(ctypes, "get_last_error", return_value=5, create=True):
            self.assertTrue(voice_lock.pid_alive(123))
        with patch.object(voice_lock.os, "name", "nt"), \
                patch.object(ctypes, "WinDLL", return_value=api, create=True), \
                patch.object(ctypes, "get_last_error", return_value=87, create=True):
            self.assertFalse(voice_lock.pid_alive(123))

        api.OpenProcess.return_value = 99

        def exit_code(value, success=True):
            def apply(_handle, output):
                ctypes.cast(output, ctypes.POINTER(ctypes.c_ulong)).contents.value = value
                return success
            return apply

        for value, success, expected in ((259, True, True), (0, True, False), (0, False, True)):
            api.GetExitCodeProcess.side_effect = exit_code(value, success)
            with patch.object(voice_lock.os, "name", "nt"), \
                    patch.object(ctypes, "WinDLL", return_value=api, create=True):
                self.assertEqual(voice_lock.pid_alive(123), expected)
        self.assertEqual(api.CloseHandle.call_count, 3)

    def test_start_time_windows_parse(self):
        with patch.object(voice_lock.os, "name", "nt"), \
                patch.object(voice_lock, "_run", return_value="1234.0"):
            self.assertEqual(voice_lock.pid_start_time_ms(7), 1234)
        with patch.object(voice_lock.os, "name", "nt"), \
                patch.object(voice_lock, "_run", return_value="not-a-time"):
            self.assertIsNone(voice_lock.pid_start_time_ms(7))

    def test_windows_guard_lock_and_unlock(self):
        fake = Mock(LK_LOCK=1, LK_UNLCK=2)
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "guard")
            with patch.object(voice_lock.os, "name", "nt"), \
                    patch.object(voice_lock, "msvcrt", fake):
                with voice_lock.Guard(path):
                    self.assertEqual(os.path.getsize(path), 1)
        self.assertEqual(
            [call.args[1] for call in fake.locking.call_args_list],
            [fake.LK_LOCK, fake.LK_UNLCK],
        )

    def test_missing_platform_lock_modules_fail_loudly(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "guard")
            with patch.object(voice_lock.os, "name", "nt"), \
                    patch.object(voice_lock, "msvcrt", None):
                with self.assertRaises(RuntimeError):
                    with voice_lock.Guard(path):
                        pass
            with patch.object(voice_lock.os, "name", "posix"), \
                    patch.object(voice_lock, "fcntl", None):
                with self.assertRaises(RuntimeError):
                    with voice_lock.Guard(path):
                        pass


if __name__ == "__main__":
    unittest.main()
