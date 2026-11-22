#!/usr/bin/env python3
"""The persisted Codex app-server choice: resolve_codex_app_server's order (env, the desktop's
runtime.json, core.codex_app_server), persist_codex_app_server's two homes, and the
`sutando-config.sh codex-app-server[-select]` subcommands."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src import sutando_config  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sutando_config._reset_cache_for_tests()
        self.addCleanup(sutando_config._reset_cache_for_tests)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ("SUTANDO_APP_SUPPORT", "SUTANDO_CODEX_APP_SERVER", "SUTANDO_DEFAULT_WORKSPACE",
                     "SUTANDO_TEST_MODE", "SUTANDO_WORKSPACE"):
            os.environ.pop(name, None)
        self.base = Path(self.tmp.name)
        # A desktop-shaped tree: <app>/engine/<checkout>, its runtime.json at <app>/runtime.json.
        self.repo = self.base / "app" / "engine" / "sutando"
        self.repo.mkdir(parents=True)
        (self.repo / "sutando.config.json").write_text("{}")
        self.workspace = self.repo / "workspace"
        self.workspace.mkdir()
        self.runtime_json = self.base / "app" / "runtime.json"

    def resolve(self):
        sutando_config._reset_cache_for_tests()
        return sutando_config.resolve_codex_app_server(self.repo)

    def layer(self, body):
        (self.workspace / "sutando.config.local.json").write_text(json.dumps(body))


class Resolve(Base):
    def test_off_by_default(self):
        self.assertFalse(self.resolve())

    def test_the_workspace_layer_turns_it_on(self):
        self.layer({"core": {"codex_app_server": True}})
        self.assertTrue(self.resolve())

    def test_the_desktop_choice_outranks_config(self):
        self.layer({"core": {"codex_app_server": True}})
        self.runtime_json.write_text(json.dumps({"coreRuntime": "codex", "codexAppServer": False}))
        self.assertFalse(self.resolve())
        self.runtime_json.write_text(json.dumps({"codexAppServer": True}))
        self.layer({})
        self.assertTrue(self.resolve())

    def test_a_non_boolean_or_unreadable_desktop_value_is_skipped(self):
        self.layer({"core": {"codex_app_server": True}})
        for body in ('{"codexAppServer": "yes"}', '{"codexAppServer": 1}', "not json", "[]"):
            self.runtime_json.write_text(body)
            self.assertTrue(self.resolve(), body)

    def test_a_non_boolean_config_value_is_off(self):
        self.layer({"core": {"codex_app_server": "true"}})
        self.assertFalse(self.resolve())

    def test_a_set_env_overrides_both(self):
        self.runtime_json.write_text(json.dumps({"codexAppServer": True}))
        os.environ["SUTANDO_CODEX_APP_SERVER"] = "0"
        self.assertFalse(self.resolve())
        self.runtime_json.write_text(json.dumps({"codexAppServer": False}))
        os.environ["SUTANDO_CODEX_APP_SERVER"] = "1"
        self.assertTrue(self.resolve())

    def test_an_empty_env_is_unset(self):
        self.runtime_json.write_text(json.dumps({"codexAppServer": True}))
        os.environ["SUTANDO_CODEX_APP_SERVER"] = ""
        self.assertTrue(self.resolve())


class Persist(Base):
    def test_a_desktop_install_writes_runtime_json_and_keeps_its_other_keys(self):
        self.runtime_json.write_text(json.dumps({"coreRuntime": "codex", "corePath": "/x"}))
        outcome, path = sutando_config.persist_codex_app_server(True, self.repo)
        self.assertEqual((outcome, path.resolve()), ("updated", self.runtime_json.resolve()))
        self.assertEqual(json.loads(self.runtime_json.read_text()),
                         {"coreRuntime": "codex", "corePath": "/x", "codexAppServer": True})
        self.assertEqual(sutando_config.persist_codex_app_server(True, self.repo)[0], "unchanged")
        self.assertTrue(self.resolve())
        self.assertEqual(sutando_config.persist_codex_app_server(False, self.repo)[0], "updated")
        self.assertFalse(self.resolve())
        self.assertFalse((self.workspace / "sutando.config.local.json").exists())

    def test_a_desktop_runtime_json_that_is_not_an_object_is_refused(self):
        self.runtime_json.write_text("[1]")
        with self.assertRaises(ValueError):
            sutando_config.persist_codex_app_server(True, self.repo)
        self.assertEqual(self.runtime_json.read_text(), "[1]")

    def test_without_a_desktop_the_workspace_layer_is_written(self):
        repo = self.base / "clone"
        (repo / "workspace").mkdir(parents=True)
        (repo / "sutando.config.json").write_text("{}")
        layer = repo / "workspace" / "sutando.config.local.json"
        layer.write_text(json.dumps({"core": {"runtime": "codex"}, "vault": {"enabled": False}}))
        outcome, path = sutando_config.persist_codex_app_server(True, repo)
        self.assertEqual((outcome, path.resolve()), ("updated", layer.resolve()))
        self.assertEqual(json.loads(layer.read_text()),
                         {"core": {"runtime": "codex", "codex_app_server": True}, "vault": {"enabled": False}})
        sutando_config._reset_cache_for_tests()
        self.assertTrue(sutando_config.resolve_codex_app_server(repo))
        self.assertEqual(sutando_config.persist_codex_app_server(True, repo)[0], "unchanged")

    def test_only_a_boolean_is_accepted(self):
        with self.assertRaises(ValueError):
            sutando_config.persist_codex_app_server("on", self.repo)  # type: ignore[arg-type]


class Cli(unittest.TestCase):
    """The subcommands in a copied checkout, with no desktop: the workspace layer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "clone"
        for rel in ("scripts/sutando-config.sh", "scripts/python-binary.sh", "src/sutando_config.py",
                    "src/__init__.py"):
            src = REPO / rel
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                shutil.copy2(src, dest)
            else:
                dest.write_text("")
        (self.root / "sutando.config.json").write_text("{}")
        (self.root / "workspace").mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("SUTANDO_APP_SUPPORT", "SUTANDO_CODEX_APP_SERVER", "SUTANDO_DEFAULT_WORKSPACE",
                                 "SUTANDO_TEST_MODE", "SUTANDO_WORKSPACE")}
        self.env["SUTANDO_PY"] = sys.executable

    def cli(self, *args, **env):
        return subprocess.run(["bash", str(self.root / "scripts/sutando-config.sh"), *args],
                              capture_output=True, text=True, env={**self.env, **env}, timeout=60)

    def test_get_select_get(self):
        self.assertEqual(self.cli("codex-app-server").stdout, "0")
        r = self.cli("codex-app-server-select", "on")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("updated "), r.stdout)
        self.assertEqual(self.cli("codex-app-server").stdout, "1")
        self.assertEqual(self.cli("codex-app-server", SUTANDO_CODEX_APP_SERVER="0").stdout, "0")
        self.assertTrue(self.cli("codex-app-server-select", "on").stdout.startswith("unchanged "))
        self.assertEqual(self.cli("codex-app-server-select", "off").returncode, 0)
        self.assertEqual(self.cli("codex-app-server").stdout, "0")

    def test_select_needs_on_or_off(self):
        for args in ((), ("yes",)):
            r = self.cli("codex-app-server-select", *args)
            self.assertEqual(r.returncode, 2)
            self.assertIn("needs on or off", r.stderr)


if __name__ == "__main__":
    unittest.main()
