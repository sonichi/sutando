#!/usr/bin/env python3
"""Unit tests for selecting the persistent Sutando core CLI."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src import sutando_config  # noqa: E402


class CoreRuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        sutando_config._reset_cache_for_tests()

    def tearDown(self):
        sutando_config._reset_cache_for_tests()
        self.tmp.cleanup()

    def write(self, payload):
        (self.repo / "sutando.config.json").write_text(json.dumps(payload))

    def test_defaults_to_claude(self):
        self.write({})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUTANDO_CORE_RUNTIME", None)
            self.assertEqual(sutando_config.resolve_core_runtime(self.repo), "claude")

    def test_config_selects_codex(self):
        self.write({"core": {"runtime": "codex"}})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUTANDO_CORE_RUNTIME", None)
            self.assertEqual(sutando_config.resolve_core_runtime(self.repo), "codex")

    def test_env_is_invocation_override(self):
        self.write({"core": {"runtime": "claude"}})
        with patch.dict(os.environ, {"SUTANDO_CORE_RUNTIME": "codex"}):
            self.assertEqual(sutando_config.resolve_core_runtime(self.repo), "codex")

    def test_rejects_unknown_runtime(self):
        self.write({"core": {"runtime": "other"}})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUTANDO_CORE_RUNTIME", None)
            with self.assertRaisesRegex(ValueError, "unsupported core.runtime"):
                sutando_config.resolve_core_runtime(self.repo)


if __name__ == "__main__":
    unittest.main()
