#!/usr/bin/env python3
"""`<workspace>/sutando.config.local.json` is a config layer above the repo files.

A desktop install replaces the engine tree (the repo) on every app update, so a
repo-root `sutando.config.local.json` is lost with it while the workspace
survives. These tests pin the layer's contract in src/sutando_config.py:
precedence, the `workspace` key it may not set, and loud failure on bad JSON.

Run: python3 tests/sutando-config-workspace-layer.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import sutando_config as sc  # noqa: E402

_ENV_KEYS = ("SUTANDO_WORKSPACE", "SUTANDO_TEST_MODE", "SUTANDO_DEFAULT_WORKSPACE")


class WorkspaceLayer(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
        sc._reset_cache_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "engine"
        self.ws = base / "durable-workspace"
        self.repo.mkdir()
        self.ws.mkdir()
        # The desktop shape: tracked config names ${REPO_DIR}/workspace, a symlink.
        (self.repo / "workspace").symlink_to(self.ws)
        self._write(self.repo / "sutando.config.json", {
            "workspace": {"path": "${REPO_DIR}/workspace"},
            "core": {"runtime": "claude", "effort": "low"},
            "vault": {"remote_url": "", "sync": {"include": ["notes/"], "exclude": ["tasks/"]}},
        })

    def tearDown(self):
        sc._reset_cache_for_tests()
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self._tmp.cleanup()

    @staticmethod
    def _write(path: Path, body) -> None:
        path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")

    def _load(self):
        sc._reset_cache_for_tests()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = sc.load_config(self.repo)
        return cfg, err.getvalue()

    def test_absent_layer_changes_nothing(self):
        cfg, err = self._load()
        self.assertEqual(cfg["core"], {"runtime": "claude", "effort": "low"})
        self.assertEqual(err, "")

    def test_layer_is_read_from_the_resolved_workspace(self):
        self._write(self.ws / "sutando.config.local.json",
                    {"vault": {"remote_url": "git@example.invalid:me/vault.git"}})
        cfg, _ = self._load()
        self.assertEqual(cfg["vault"]["remote_url"], "git@example.invalid:me/vault.git")
        self.assertEqual(cfg["vault"]["sync"]["include"], ["notes/"],
                         "dicts deep-merge: untouched sibling keys survive")

    def test_precedence_workspace_over_repo_local_over_tracked(self):
        self._write(self.repo / "sutando.config.local.json",
                    {"core": {"runtime": "codex", "effort": "high"}})
        self._write(self.ws / "sutando.config.local.json", {"core": {"effort": "max"}})
        cfg, _ = self._load()
        self.assertEqual(cfg["core"]["effort"], "max", "workspace layer beats repo-local")
        self.assertEqual(cfg["core"]["runtime"], "codex", "repo-local still beats tracked")

    def test_arrays_replace_and_exclude_extra_stays_additive(self):
        self._write(self.ws / "sutando.config.local.json", {"vault": {"sync": {
            "include": ["notes/", "hosts/*/"], "exclude_extra": ["notes/private/"]}}})
        sc._reset_cache_for_tests()
        v = sc.resolve_vault(self.repo)
        self.assertEqual(v["sync"]["include"], ["notes/", "hosts/*/"])
        self.assertEqual(v["sync"]["exclude"], ["tasks/", "notes/private/"])

    def test_workspace_key_is_dropped_with_a_warning(self):
        self._write(self.ws / "sutando.config.local.json",
                    {"workspace": {"path": "/somewhere/else"}, "core": {"effort": "max"}})
        cfg, err = self._load()
        self.assertEqual(cfg["workspace"]["path"], f"{self.repo}/workspace")
        self.assertEqual(cfg["core"]["effort"], "max", "the rest of the layer still applies")
        self.assertIn("sets 'workspace', which it cannot change", err)
        self.assertNotIn("/somewhere/else", err, "no path-shaped stderr")
        sc._reset_cache_for_tests()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sc.resolve_workspace(self.repo), self.ws.resolve())

    def test_malformed_layer_raises_naming_the_file(self):
        self._write(self.ws / "sutando.config.local.json", '{"vault": ')
        with self.assertRaises(RuntimeError) as cm:
            self._load()
        self.assertIn(str(self.ws.resolve() / "sutando.config.local.json"), str(cm.exception))

    def test_scalar_block_in_layer_is_rejected_like_repo_local(self):
        self._write(self.ws / "sutando.config.local.json", '{"vault": "nope"}')
        with self.assertRaises(RuntimeError) as cm:
            self._load()
        self.assertIn("key 'vault' must be a JSON object", str(cm.exception))

    def test_empty_layer_is_tolerated(self):
        self._write(self.ws / "sutando.config.local.json", "  \n")
        cfg, _ = self._load()
        self.assertEqual(cfg["core"]["effort"], "low")

    def test_workspace_at_repo_root_reads_the_local_file_once(self):
        # workspace.path = the repo root makes the two files one; re-reading it as the
        # layer would drop its own `workspace` key and warn.
        self._write(self.repo / "sutando.config.local.json",
                    {"workspace": {"path": "${REPO_DIR}"}, "core": {"effort": "high"}})
        cfg, err = self._load()
        self.assertEqual(cfg["workspace"]["path"], str(self.repo))
        self.assertEqual(err, "", "the repo-local file is not re-read as a workspace layer")

    def test_embedder_default_workspace_is_where_the_layer_is_read(self):
        (self.repo / "sutando.config.json").write_text("{}", encoding="utf-8")
        other = Path(self._tmp.name) / "embedder-ws"
        other.mkdir()
        self._write(other / "sutando.config.local.json", {"core": {"effort": "max"}})
        os.environ["SUTANDO_DEFAULT_WORKSPACE"] = str(other)
        cfg, _ = self._load()
        self.assertEqual(cfg["core"]["effort"], "max")

    def test_layer_follows_a_configured_workspace_path(self):
        moved = Path(self._tmp.name) / "moved-ws"
        moved.mkdir()
        self._write(self.repo / "sutando.config.local.json", {"workspace": {"path": str(moved)}})
        self._write(self.ws / "sutando.config.local.json", {"core": {"effort": "WRONG"}})
        self._write(moved / "sutando.config.local.json", {"core": {"effort": "max"}})
        cfg, _ = self._load()
        self.assertEqual(cfg["core"]["effort"], "max")


if __name__ == "__main__":
    unittest.main(verbosity=2)
