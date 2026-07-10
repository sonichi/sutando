#!/usr/bin/env python3
"""Delegator: run src/remote-gateway-bridge.test.py under the tests/ discovery
root.

The coverage gate (scripts/coverage-gate.sh) only discovers tests/**/*.test.py,
so the bridge's own suite — which lives next to the module in src/ — never ran
under coverage and its lines counted as uncovered in diff-cover. Running it
in-process (runpy, not a subprocess) keeps execution inside the same coverage
recorder.

Run: python3 tests/remote-gateway-bridge.test.py
Exit code: propagated from the src suite (0 pass, 1 fail).
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "src" / "remote-gateway-bridge.test.py"

try:
    runpy.run_path(str(TARGET), run_name="__main__")
except SystemExit as e:
    sys.exit(e.code)
sys.exit(0)
