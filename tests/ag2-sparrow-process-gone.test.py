#!/usr/bin/env python3
"""Hatch's process_gone reads a pid that still answers kill(0) by its /proc state: a zombie (Z) and
one its parent is reaping (X, or x on older kernels) have exited; a running or sleeping one has not.
Run: python3 tests/ag2-sparrow-process-gone.test.py"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow.adapters.hatch import cli as hatch_cli  # noqa: E402


class ProcessGone(unittest.TestCase):
    def gone(self, state: bytes) -> bool:
        pid = os.getpid()
        stat = b"%d (ag2-sparrow) %s 1 2 3" % (pid, state)
        with mock.patch.object(hatch_cli, "_proc_stat", return_value=stat), \
                mock.patch.object(hatch_cli.os, "kill", return_value=None):
            return hatch_cli.process_gone(pid)

    def test_exited_states_are_gone(self):
        for state in (b"Z", b"X", b"x"):
            with self.subTest(state=state):
                self.assertTrue(self.gone(state))

    def test_live_states_are_not_gone(self):
        for state in (b"R", b"S", b"D"):
            with self.subTest(state=state):
                self.assertFalse(self.gone(state))

    def test_a_comm_with_parens_and_spaces_is_read_after_the_last_paren(self):
        pid = os.getpid()
        stat = b"%d (a ) X b) S 1 2 3" % pid
        with mock.patch.object(hatch_cli, "_proc_stat", return_value=stat), \
                mock.patch.object(hatch_cli.os, "kill", return_value=None):
            self.assertFalse(hatch_cli.process_gone(pid))


if __name__ == "__main__":
    unittest.main()
