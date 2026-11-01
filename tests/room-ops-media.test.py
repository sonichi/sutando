#!/usr/bin/env python3
"""Run the skill's media contract tests in the coverage-instrumented standalone lane."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"))
from test_room_ops import MediaTests  # noqa: E402,F401

if __name__ == "__main__":
    unittest.main()
