#!/usr/bin/env python3
"""Tests for obsidian-mirror.py's opt-in gate.

main() exits cleanly (return 0, "not enabled" message) when
SUTANDO_OBSIDIAN_MIRROR is unset and --force is absent. The gate is resolved
via config_get() (issue #1724 migration); this exercises that call site, which
was otherwise uncovered.
"""
import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
SCRIPT = SRC / "obsidian-mirror.py"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load():
    spec = importlib.util.spec_from_file_location("obsidian_mirror", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _env_without(*keys):
    return {k: v for k, v in os.environ.items() if k not in keys}


class TestObsidianMirrorGate(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_disabled_gate_exits_zero(self):
        base = _env_without("SUTANDO_OBSIDIAN_MIRROR")
        buf = io.StringIO()
        with patch.dict(os.environ, base, clear=True), redirect_stdout(buf):
            rc = self.mod.main([])
        self.assertEqual(rc, 0)
        self.assertIn("not enabled", buf.getvalue())

    def test_disabled_value_off_exits_zero(self):
        # An explicit falsey value ("0") is still the disabled path.
        base = _env_without("SUTANDO_OBSIDIAN_MIRROR")
        base["SUTANDO_OBSIDIAN_MIRROR"] = "0"
        buf = io.StringIO()
        with patch.dict(os.environ, base, clear=True), redirect_stdout(buf):
            rc = self.mod.main([])
        self.assertEqual(rc, 0)
        self.assertIn("not enabled", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
