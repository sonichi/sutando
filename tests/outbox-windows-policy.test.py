#!/usr/bin/env python3
"""Cross-platform unit coverage for the Windows outbox identity backend."""

from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import outbox  # noqa: E402


class WindowsIdentityPolicy(unittest.TestCase):
    def tearDown(self):
        outbox._windows_api.cache_clear()

    def test_api_binding(self):
        api = Mock()
        api.OpenProcess = Mock()
        api.WaitForSingleObject = Mock()
        api.GetProcessTimes = Mock()
        api.CloseHandle = Mock()
        with patch.object(ctypes, "WinDLL", return_value=api, create=True):
            self.assertIs(outbox._windows_api(), api)
        self.assertIsNotNone(api.OpenProcess.argtypes)
        self.assertIsNotNone(api.OpenProcess.restype)
        self.assertIsNotNone(api.WaitForSingleObject.argtypes)
        self.assertIsNotNone(api.GetProcessTimes.argtypes)
        self.assertIsNotNone(api.CloseHandle.argtypes)

    def test_input_and_api_failures_are_unknown(self):
        for pid in (0, -1, 0x100000000, "123", None):
            with self.subTest(pid=pid), patch.object(
                    outbox, "_windows_api", side_effect=AssertionError):
                self.assertEqual(
                    outbox._windows_process_identity(pid).state,
                    outbox.OwnerState.UNKNOWN,
                )
        for error in (OSError("unavailable"), AttributeError("missing")):
            with patch.object(outbox, "_windows_api", side_effect=error):
                self.assertEqual(
                    outbox._windows_process_identity(123).state,
                    outbox.OwnerState.UNKNOWN,
                )

    def test_open_and_wait_outcomes(self):
        for error, state in (
            (87, outbox.OwnerState.DEAD),
            (5, outbox.OwnerState.UNKNOWN),
        ):
            api = Mock()
            api.OpenProcess.return_value = None
            with patch.object(outbox, "_windows_api", return_value=api), \
                    patch.object(ctypes, "get_last_error", return_value=error, create=True):
                self.assertEqual(outbox._windows_process_identity(123).state, state)

        for wait, state in (
            (0, outbox.OwnerState.DEAD),
            (0xFFFFFFFF, outbox.OwnerState.UNKNOWN),
            (128, outbox.OwnerState.UNKNOWN),
        ):
            api = Mock()
            api.OpenProcess.return_value = 99
            api.WaitForSingleObject.return_value = wait
            with patch.object(outbox, "_windows_api", return_value=api):
                self.assertEqual(outbox._windows_process_identity(123).state, state)
            api.CloseHandle.assert_called_once_with(99)

    def test_process_times_and_dispatch(self):
        from ctypes import wintypes

        api = Mock()
        api.OpenProcess.return_value = 99
        api.WaitForSingleObject.return_value = 258
        api.GetProcessTimes.return_value = False
        with patch.object(outbox, "_windows_api", return_value=api):
            self.assertEqual(
                outbox._windows_process_identity(123).state,
                outbox.OwnerState.UNKNOWN,
            )

        ticks = 116444736000000000 + 1234567890

        def write_times(_handle, created, *_unused):
            value = ctypes.cast(created, ctypes.POINTER(wintypes.FILETIME)).contents
            value.dwHighDateTime = ticks >> 32
            value.dwLowDateTime = ticks & 0xFFFFFFFF
            return True

        api.GetProcessTimes.side_effect = write_times
        with patch.object(outbox, "_windows_api", return_value=api):
            self.assertEqual(
                outbox._windows_process_identity(123),
                outbox.ProcessIdentity(123, outbox.OwnerState.ALIVE, 123456789),
            )

        expected = outbox.ProcessIdentity(7, outbox.OwnerState.ALIVE, 11)
        with patch.object(outbox.os, "name", "nt"), \
                patch.object(outbox, "_windows_process_identity", return_value=expected):
            self.assertEqual(outbox.process_identity(7), expected)


if __name__ == "__main__":
    unittest.main()
