#!/usr/bin/env python3
"""Run the package identity contract in the coverage-producing standalone lane."""
from pathlib import Path
import runpy

runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "packages/ag2-sparrow/tests/test_identity.py"),
    run_name="__main__",
)
