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
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SUTANDO_APP_SUPPORT", None)

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

    def test_desktop_selection_outranks_config_but_not_env(self):
        """Jobs started without the desktop's environment still follow its runtime.json."""
        repo = self.repo / "app" / "engine" / "sutando"
        repo.mkdir(parents=True)
        (repo / "sutando.config.json").write_text(json.dumps({"core": {"runtime": "claude"}}))
        selection = self.repo / "app" / "runtime.json"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUTANDO_CORE_RUNTIME", None)
            os.environ.pop("SUTANDO_APP_SUPPORT", None)
            self.assertEqual(sutando_config.resolve_core_runtime(repo), "claude")
            selection.write_text(json.dumps({"coreRuntime": "codex"}))
            self.assertEqual(sutando_config.resolve_core_runtime(repo), "codex")
            os.environ["SUTANDO_CORE_RUNTIME"] = "claude"
            self.assertEqual(sutando_config.resolve_core_runtime(repo), "claude")
            os.environ.pop("SUTANDO_CORE_RUNTIME")
            for raw in ("{", json.dumps({"coreRuntime": "fish"}), json.dumps(["codex"])):
                selection.write_text(raw)
                self.assertEqual(sutando_config.resolve_core_runtime(repo), "claude")

    def test_exported_app_support_names_the_selection(self):
        self.write({"core": {"runtime": "claude"}})
        app = self.repo / "elsewhere"
        app.mkdir()
        (app / "runtime.json").write_text(json.dumps({"coreRuntime": "codex"}))
        with patch.dict(os.environ, {"SUTANDO_APP_SUPPORT": str(app)}, clear=False):
            os.environ.pop("SUTANDO_CORE_RUNTIME", None)
            self.assertEqual(sutando_config.resolve_core_runtime(self.repo), "codex")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUTANDO_CORE_RUNTIME", None)
            os.environ.pop("SUTANDO_APP_SUPPORT", None)
            self.assertEqual(sutando_config.resolve_core_runtime(self.repo), "claude")

    def test_rejects_unknown_runtime(self):
        self.write({"core": {"runtime": "other"}})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUTANDO_CORE_RUNTIME", None)
            with self.assertRaisesRegex(ValueError, "unsupported core.runtime"):
                sutando_config.resolve_core_runtime(self.repo)


if __name__ == "__main__":
    unittest.main()
