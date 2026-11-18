#!/usr/bin/env python3
"""friction-detector's daily report reaches results/ through the shared publisher (#3956).

A drain claims results/*.txt on sight, so the report must appear whole, never as an
empty name filled afterwards. Every check is stubbed; only the write path is under test.

Run: python3 tests/friction-detector-publishes-whole.test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src" / "friction-detector.py"
CLEAN = "No friction detected today. Everything is clean."


class FrictionDetectorPublishes(unittest.TestCase):
    def test_report_lands_whole_through_the_publisher(self):
        spec = importlib.util.spec_from_file_location("fd_publish", SRC)
        fd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fd)
        with tempfile.TemporaryDirectory() as td:
            fd.RESULTS_DIR = Path(td)
            checks = [n for n in dir(fd) if n.startswith("check_")]
            with contextlib.ExitStack() as stack:
                pub = stack.enter_context(mock.patch.object(fd, "publish_text", wraps=fd.publish_text))
                for name in checks:
                    stack.enter_context(mock.patch.object(fd, name, return_value=[]))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                fd.main()
            (report,) = Path(td).iterdir()
            pub.assert_called_once_with(report, CLEAN)
            self.assertEqual(report.read_text(), CLEAN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
