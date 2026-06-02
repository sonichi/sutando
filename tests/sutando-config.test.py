#!/usr/bin/env python3
"""Tests for src/sutando_config.py — the canonical workspace + vault loader.

Covers the eight invariants Mini called out in the cold review of #1395:
  1. $SUTANDO_WORKSPACE env var precedence over .local.json
  2. .local.json deep-merge over tracked sutando.config.json
     (dicts merge, arrays REPLACE wholesale)
  3. ${REPO_DIR} expansion in string values, NOT in keys
  4. _-prefixed comment keys stripped before validation
  5. Malformed JSON → clear RuntimeError naming the file + line/col
  6. Empty .local.json (freshly-touched) treated as {}
  7. Cache reset across repo_root changes (per-process cache invalidates)
  8. resolve_vault() returns safe defaults when vault subtree absent

Run: python3 tests/sutando-config.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import sutando_config  # noqa: E402
from sutando_config import (  # noqa: E402
    _deep_merge,
    _expand_vars,
    _reset_cache_for_tests,
    _strip_comments,
    detect_env_workspace_in_dotenv,
    load_config,
    resolve_vault,
    resolve_workspace,
)


def _write_config(repo: Path, name: str, body: dict | str) -> Path:
    """Write a config file under `repo` and return its path.

    `body` may be a dict (json-dumped) or a raw string (written verbatim,
    used for malformed-JSON test cases).
    """
    path = repo / name
    if isinstance(body, dict):
        path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    else:
        path.write_text(body, encoding="utf-8")
    return path


class TestSutandoConfig(unittest.TestCase):
    """Loader unit tests, each in an isolated tmp repo."""

    def setUp(self):
        # Stash any env var that could leak resolution between tests.
        self._saved_env = os.environ.pop("SUTANDO_WORKSPACE", None)
        _reset_cache_for_tests()
        # Each test gets its own tmp dir simulating a Sutando checkout.
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        _reset_cache_for_tests()
        os.environ.pop("SUTANDO_WORKSPACE", None)
        if self._saved_env is not None:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_env
        self._tmp.cleanup()

    # ------------------------------------------------------------------ #
    #  1. Env var precedence over .local.json                            #
    # ------------------------------------------------------------------ #

    def test_env_var_precedence_over_local_json(self):
        _write_config(self.repo, "sutando.config.json",
                      {"workspace": {"path": "${REPO_DIR}/workspace"}})
        _write_config(self.repo, "sutando.config.local.json",
                      {"workspace": {"path": "/from/local"}})
        os.environ["SUTANDO_WORKSPACE"] = "/from/env"
        resolved = resolve_workspace(repo_root=self.repo)
        self.assertEqual(str(resolved), str(Path("/from/env").resolve()))

    # ------------------------------------------------------------------ #
    #  2. Deep-merge: dicts merge, arrays REPLACE                        #
    # ------------------------------------------------------------------ #

    def test_local_deep_merge_dicts(self):
        defaults = {
            "workspace": {"path": "${REPO_DIR}/workspace"},
            "vault": {"enabled": False, "remote_url": "", "interval_seconds": 1800},
        }
        local = {"vault": {"enabled": True, "remote_url": "https://vault.example/repo.git"}}
        _write_config(self.repo, "sutando.config.json", defaults)
        _write_config(self.repo, "sutando.config.local.json", local)
        cfg = load_config(repo_root=self.repo)
        # Dict keys present in BOTH (vault.enabled + vault.remote_url) take the local value;
        # keys only in defaults (vault.interval_seconds) survive.
        self.assertEqual(cfg["vault"]["enabled"], True)
        self.assertEqual(cfg["vault"]["remote_url"], "https://vault.example/repo.git")
        self.assertEqual(cfg["vault"]["interval_seconds"], 1800)

    def test_local_replaces_arrays_wholesale(self):
        defaults = {
            "vault": {"sync": {"include": ["notes/", "memory/", "skills/"],
                               "exclude": ["tasks/", "logs/"]}}
        }
        # .local.json overrides include with a SHORTER list; should fully replace,
        # not union.
        local = {"vault": {"sync": {"include": ["notes/"]}}}
        _write_config(self.repo, "sutando.config.json", defaults)
        _write_config(self.repo, "sutando.config.local.json", local)
        cfg = load_config(repo_root=self.repo)
        self.assertEqual(cfg["vault"]["sync"]["include"], ["notes/"])
        # exclude wasn't overridden → original survives
        self.assertEqual(cfg["vault"]["sync"]["exclude"], ["tasks/", "logs/"])

    # ------------------------------------------------------------------ #
    #  3. ${REPO_DIR} expansion in values, NOT keys                      #
    # ------------------------------------------------------------------ #

    def test_repo_dir_expansion_in_values(self):
        _write_config(self.repo, "sutando.config.json",
                      {"workspace": {"path": "${REPO_DIR}/workspace"}})
        cfg = load_config(repo_root=self.repo)
        self.assertEqual(cfg["workspace"]["path"], f"{self.repo}/workspace")

    def test_repo_dir_token_in_key_is_not_expanded(self):
        """A `${REPO_DIR}` token used as a KEY name must NOT expand.

        The loader walks dicts but only swaps the token in scalar string VALUES.
        This is a regression guard — accidental key expansion would silently
        rename config sections.
        """
        _write_config(self.repo, "sutando.config.json",
                      {"workspace": {"path": "${REPO_DIR}/ws"},
                       "${REPO_DIR}": "this key should not expand"})
        cfg = load_config(repo_root=self.repo)
        self.assertIn("${REPO_DIR}", cfg)
        self.assertEqual(cfg["${REPO_DIR}"], "this key should not expand")
        self.assertEqual(cfg["workspace"]["path"], f"{self.repo}/ws")

    # ------------------------------------------------------------------ #
    #  4. _-prefixed comment keys stripped                                #
    # ------------------------------------------------------------------ #

    def test_underscore_keys_stripped(self):
        _write_config(self.repo, "sutando.config.json", {
            "_comment": "this is documentation, not config",
            "_another": {"nested": "also dropped"},
            "workspace": {"_comment": "nested annotation", "path": "/ws"},
        })
        cfg = load_config(repo_root=self.repo)
        self.assertNotIn("_comment", cfg)
        self.assertNotIn("_another", cfg)
        self.assertNotIn("_comment", cfg["workspace"])
        self.assertEqual(cfg["workspace"]["path"], "/ws")

    # ------------------------------------------------------------------ #
    #  5. Malformed JSON → RuntimeError with file + line/col              #
    # ------------------------------------------------------------------ #

    def test_malformed_json_raises_runtime_error(self):
        _write_config(self.repo, "sutando.config.json", "{ this is not JSON }")
        with self.assertRaises(RuntimeError) as ctx:
            load_config(repo_root=self.repo)
        msg = str(ctx.exception)
        self.assertIn("sutando.config.json", msg)
        # parse-error message should name where it failed
        self.assertIn("line", msg.lower())

    def test_non_object_top_level_raises_runtime_error(self):
        _write_config(self.repo, "sutando.config.json", "[1, 2, 3]")
        with self.assertRaises(RuntimeError) as ctx:
            load_config(repo_root=self.repo)
        self.assertIn("JSON object", str(ctx.exception))

    # ------------------------------------------------------------------ #
    #  6. Empty .local.json treated as {}                                 #
    # ------------------------------------------------------------------ #

    def test_empty_local_json_treated_as_empty_dict(self):
        _write_config(self.repo, "sutando.config.json",
                      {"workspace": {"path": "${REPO_DIR}/workspace"}})
        (self.repo / "sutando.config.local.json").touch()  # zero-byte file
        cfg = load_config(repo_root=self.repo)
        self.assertEqual(cfg["workspace"]["path"], f"{self.repo}/workspace")

    def test_whitespace_only_local_json_treated_as_empty_dict(self):
        _write_config(self.repo, "sutando.config.json",
                      {"workspace": {"path": "${REPO_DIR}/workspace"}})
        (self.repo / "sutando.config.local.json").write_text("   \n\n  \n", encoding="utf-8")
        cfg = load_config(repo_root=self.repo)
        self.assertEqual(cfg["workspace"]["path"], f"{self.repo}/workspace")

    def test_missing_local_json_treated_as_empty_dict(self):
        _write_config(self.repo, "sutando.config.json",
                      {"workspace": {"path": "/from/defaults"}})
        cfg = load_config(repo_root=self.repo)
        self.assertEqual(cfg["workspace"]["path"], "/from/defaults")

    # ------------------------------------------------------------------ #
    #  7. Cache reset across repo_root changes                            #
    # ------------------------------------------------------------------ #

    def test_cache_reload_when_repo_root_changes(self):
        # Two repos with DIFFERENT configs; loading from each must return
        # the matching config (proves cache is keyed correctly, not memoized
        # globally).
        repo_a = Path(self._tmp.name) / "a"
        repo_b = Path(self._tmp.name) / "b"
        repo_a.mkdir()
        repo_b.mkdir()
        _write_config(repo_a, "sutando.config.json", {"workspace": {"path": "/from/a"}})
        _write_config(repo_b, "sutando.config.json", {"workspace": {"path": "/from/b"}})
        cfg_a = load_config(repo_root=repo_a)
        cfg_b = load_config(repo_root=repo_b)
        self.assertEqual(cfg_a["workspace"]["path"], "/from/a")
        self.assertEqual(cfg_b["workspace"]["path"], "/from/b")

    def test_cache_reused_when_repo_root_unchanged(self):
        _write_config(self.repo, "sutando.config.json", {"workspace": {"path": "/x"}})
        first = load_config(repo_root=self.repo)
        # Mutate the file post-cache to prove the cache is being USED.
        # A re-load without cache reset must return the cached value.
        _write_config(self.repo, "sutando.config.json", {"workspace": {"path": "/y"}})
        second = load_config(repo_root=self.repo)
        self.assertIs(first, second)  # same dict object → cache hit
        # After reset, the new file content shows up.
        _reset_cache_for_tests()
        third = load_config(repo_root=self.repo)
        self.assertEqual(third["workspace"]["path"], "/y")

    # ------------------------------------------------------------------ #
    #  8. resolve_vault() safe defaults                                   #
    # ------------------------------------------------------------------ #

    def test_resolve_vault_safe_defaults(self):
        _write_config(self.repo, "sutando.config.json", {})  # no vault subtree
        vault = resolve_vault(repo_root=self.repo)
        self.assertEqual(vault["enabled"], False)
        self.assertEqual(vault["remote_url"], "")
        self.assertEqual(vault["sync"]["include"], [])
        self.assertEqual(vault["sync"]["exclude"], [])
        self.assertEqual(vault["interval_seconds"], 1800)

    def test_resolve_vault_overrides_propagate(self):
        _write_config(self.repo, "sutando.config.json", {
            "vault": {
                "enabled": True,
                "remote_url": "https://vault.example/x.git",
                "sync": {"include": ["notes/"], "exclude": ["tasks/"]},
                "interval_seconds": 600,
            },
        })
        vault = resolve_vault(repo_root=self.repo)
        self.assertEqual(vault["enabled"], True)
        self.assertEqual(vault["remote_url"], "https://vault.example/x.git")
        self.assertEqual(vault["sync"]["include"], ["notes/"])
        self.assertEqual(vault["sync"]["exclude"], ["tasks/"])
        self.assertEqual(vault["interval_seconds"], 600)

    # ------------------------------------------------------------------ #
    #  Bonus: detect_env_workspace_in_dotenv()                            #
    # ------------------------------------------------------------------ #

    def test_detect_env_workspace_in_dotenv_finds_line(self):
        _write_config(self.repo, "sutando.config.json", {})
        (self.repo / ".env").write_text(
            "SOMETHING_ELSE=foo\nSUTANDO_WORKSPACE=/from/dotenv\n",
            encoding="utf-8",
        )
        val = detect_env_workspace_in_dotenv(repo_root=self.repo)
        self.assertEqual(val, "/from/dotenv")

    def test_detect_env_workspace_in_dotenv_handles_quotes(self):
        _write_config(self.repo, "sutando.config.json", {})
        (self.repo / ".env").write_text(
            'SUTANDO_WORKSPACE="/quoted/path"\n', encoding="utf-8",
        )
        val = detect_env_workspace_in_dotenv(repo_root=self.repo)
        self.assertEqual(val, "/quoted/path")

    def test_detect_env_workspace_in_dotenv_returns_none_when_absent(self):
        _write_config(self.repo, "sutando.config.json", {})
        (self.repo / ".env").write_text("OTHER_VAR=foo\n", encoding="utf-8")
        self.assertIsNone(detect_env_workspace_in_dotenv(repo_root=self.repo))

    # ------------------------------------------------------------------ #
    #  Internal helpers (direct unit coverage for the small ones)         #
    # ------------------------------------------------------------------ #

    def test_strip_comments_recursive(self):
        out = _strip_comments({
            "_top": "drop",
            "kept": {"_nested": "drop", "still_here": [{"_inside": "drop", "ok": 1}]},
        })
        self.assertEqual(out, {"kept": {"still_here": [{"ok": 1}]}})

    def test_deep_merge_replaces_arrays(self):
        base = {"a": [1, 2, 3], "b": {"x": 1, "y": 2}}
        ov = {"a": [9], "b": {"y": 99, "z": 100}}
        out = _deep_merge(base, ov)
        self.assertEqual(out["a"], [9])  # array replaced
        self.assertEqual(out["b"], {"x": 1, "y": 99, "z": 100})  # dict merged

    def test_expand_vars_walks_nested_structures(self):
        out = _expand_vars({"path": "${REPO_DIR}/ws",
                            "list": ["${REPO_DIR}/a", {"k": "${REPO_DIR}/b"}],
                            "scalar_int": 42},
                           repo_dir=Path("/tmp/repo"))
        self.assertEqual(out["path"], "/tmp/repo/ws")
        self.assertEqual(out["list"][0], "/tmp/repo/a")
        self.assertEqual(out["list"][1]["k"], "/tmp/repo/b")
        self.assertEqual(out["scalar_int"], 42)  # non-string passthrough


if __name__ == "__main__":
    unittest.main(verbosity=2)
