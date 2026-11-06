#!/usr/bin/env python3
"""Run the package-canonical claude-code connect suite under repository coverage."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

if os.name == "nt":
    print("ok - package claude-code connect suite is covered by the Linux coverage job")
    raise SystemExit(0)

TARGET = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "ag2-sparrow"
    / "tests"
    / "test_claude_code_connect.py"
)

try:
    runpy.run_path(str(TARGET), run_name="__main__")
except SystemExit as exc:
    raise SystemExit(exc.code)
raise SystemExit(0)
