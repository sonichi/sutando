#!/usr/bin/env python3
"""Run part of the package-canonical multi-agent install suite under repository coverage: the per-agent fence.
The suite is split in three files so each stays inside the per-file CI time cap."""

from __future__ import annotations

import os
import runpy
import sys
import unittest
from pathlib import Path

if os.name == "nt":
    print("ok - package multi-agent suite is covered by the Linux coverage job")
    raise SystemExit(0)

TARGET = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "ag2-sparrow"
    / "tests"
    / "test_local_multi_agent.py"
)
PARTS = {"main": ("SideBySide", "Round4", "Round5"), "repair": ("Round6", "Round7", "Selection"), "fences": ("Round8",)}
CLASSES = PARTS["fences"]

suite = runpy.run_path(str(TARGET), run_name="ag2_sparrow_local_multi_agent")
loader = unittest.TestLoader()
named = {n for n, v in suite.items() if isinstance(v, type) and issubclass(v, unittest.TestCase)
         and loader.getTestCaseNames(v)}
if named != {n for part in PARTS.values() for n in part}:
    print(f"error: the three parts must cover the suite's test classes exactly: {sorted(named)}", file=sys.stderr)
    raise SystemExit(1)
tests = unittest.TestSuite(loader.loadTestsFromTestCase(suite[name]) for name in CLASSES)
result = unittest.TextTestRunner(verbosity=1).run(tests)
raise SystemExit(0 if result.wasSuccessful() else 1)
