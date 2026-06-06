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
    find_core_config_dir,
    resolve_claude_sutando_config_dir,
    resolve_core_config_dirs,
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
    #  1. v0.8: env var IGNORED; .local.json wins                         #
    # ------------------------------------------------------------------ #

    def test_env_var_ignored_in_favor_of_local_json(self):
        # v0.8 contract: `$SUTANDO_WORKSPACE` is no longer honored.
        # Setting it must NOT override `sutando.config.local.json`; the
        # resolver emits a one-time deprecation warning and returns the
        # config-resolved path. Test guards against accidental re-enable of
        # the legacy precedence (which was the v0.7 / M0 / M1 behavior).
        _write_config(self.repo, "sutando.config.json",
                      {"workspace": {"path": "${REPO_DIR}/workspace"}})
        _write_config(self.repo, "sutando.config.local.json",
                      {"workspace": {"path": "/from/local"}})
        os.environ["SUTANDO_WORKSPACE"] = "/from/env"
        # SUTANDO_TEST_MODE is NOT set here — we test the production code
        # path that ignores the env var.
        os.environ.pop("SUTANDO_TEST_MODE", None)
        resolved = resolve_workspace(repo_root=self.repo)
        self.assertEqual(str(resolved), str(Path("/from/local").resolve()))

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
    #  9. resolve_claude_sutando_config_dir() — M2 alias target          #
    # ------------------------------------------------------------------ #

    def test_claude_sutando_config_dir_default(self):
        _write_config(self.repo, "sutando.config.json", {})
        ccd = resolve_claude_sutando_config_dir(repo_root=self.repo)
        ws = resolve_workspace(repo_root=self.repo)
        # Default subdir is `.claude-sutando` under workspace.
        self.assertEqual(ccd, (ws / ".claude-sutando").resolve())

    def test_claude_sutando_config_dir_subdir_override(self):
        _write_config(self.repo, "sutando.config.json", {
            "claude_sutando_config_dir": {"subdir": "my-claude-state"},
        })
        ccd = resolve_claude_sutando_config_dir(repo_root=self.repo)
        ws = resolve_workspace(repo_root=self.repo)
        self.assertEqual(ccd, (ws / "my-claude-state").resolve())

    def test_claude_sutando_config_dir_rejects_absolute(self):
        _write_config(self.repo, "sutando.config.json", {
            "claude_sutando_config_dir": {"subdir": "/etc/claude-state"},
        })
        with self.assertRaises(ValueError) as cm:
            resolve_claude_sutando_config_dir(repo_root=self.repo)
        self.assertIn("workspace-sub-folder invariant", str(cm.exception))

    def test_claude_sutando_config_dir_rejects_parent_escape(self):
        _write_config(self.repo, "sutando.config.json", {
            "claude_sutando_config_dir": {"subdir": "../escape"},
        })
        with self.assertRaises(ValueError) as cm:
            resolve_claude_sutando_config_dir(repo_root=self.repo)
        self.assertIn("workspace-sub-folder invariant", str(cm.exception))

    def test_claude_sutando_config_dir_rejects_deep_parent_escape(self):
        # Even a `..` segment mid-path should be caught (not just at the start).
        _write_config(self.repo, "sutando.config.json", {
            "claude_sutando_config_dir": {"subdir": "ok/../../../etc"},
        })
        with self.assertRaises(ValueError) as cm:
            resolve_claude_sutando_config_dir(repo_root=self.repo)
        self.assertIn("workspace-sub-folder invariant", str(cm.exception))

    def test_claude_sutando_config_dir_lands_at_config_workspace(self):
        # v0.8: `$SUTANDO_WORKSPACE` is no longer honored. The previous
        # version of this test asserted ccd followed the env-overridden
        # workspace; now it must follow the config-resolved workspace.
        # The string-prefix invariant the caller relies on (ccd is always
        # `<workspace>/.claude-sutando`) still holds.
        with tempfile.TemporaryDirectory() as ws_dir:
            old_env = os.environ.get("SUTANDO_WORKSPACE")
            os.environ["SUTANDO_WORKSPACE"] = ws_dir  # set, but should be ignored
            os.environ.pop("SUTANDO_TEST_MODE", None)
            try:
                _write_config(self.repo, "sutando.config.json", {})
                _reset_cache_for_tests()
                ws = resolve_workspace(repo_root=self.repo)
                ccd = resolve_claude_sutando_config_dir(repo_root=self.repo)
                # Workspace falls back to the baked-in default (env ignored):
                self.assertEqual(str(ws), str((self.repo / "workspace").resolve()))
                # Ccd lands under the resolved workspace, not env:
                self.assertEqual(str(ccd), str(ws / ".claude-sutando"))
                # String-prefix invariant the caller relies on:
                self.assertTrue(str(ccd).startswith(str(ws)))
            finally:
                if old_env is None:
                    del os.environ["SUTANDO_WORKSPACE"]
                else:
                    os.environ["SUTANDO_WORKSPACE"] = old_env
                _reset_cache_for_tests()

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

    # ------------------------------------------------------------------ #
    #  Mini #8: warn on unknown top-level keys                            #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    #  Mini follow-up: SUTANDO_DEBUG strict "1" gating                    #
    # ------------------------------------------------------------------ #

    def test_debug_log_strict_equals_one(self):
        """SUTANDO_DEBUG must equal exactly "1" to emit the find-repo-root
        diagnostic — otherwise "0" / "false" / "" would silently emit, which
        is Mini's review point on the #1397 follow-up loop.
        """
        from sutando_config import _find_repo_root
        nowhere = self.repo / "deep" / "nested" / "leaf"
        nowhere.mkdir(parents=True)
        import io
        for env_val, expect_emit in [
            (None, False),       # unset
            ("0", False),        # disabled (Mini's bug-class call)
            ("false", False),    # disabled
            ("", False),         # empty
            ("1", True),         # enabled
        ]:
            with self.subTest(env_val=env_val):
                if env_val is None:
                    os.environ.pop("SUTANDO_DEBUG", None)
                else:
                    os.environ["SUTANDO_DEBUG"] = env_val
                buf = io.StringIO()
                saved_stderr = sys.stderr
                sys.stderr = buf
                try:
                    _find_repo_root(start=nowhere)
                finally:
                    sys.stderr = saved_stderr
                    os.environ.pop("SUTANDO_DEBUG", None)
                if expect_emit:
                    self.assertIn("did not find sutando.config.json", buf.getvalue())
                else:
                    self.assertEqual(buf.getvalue(), "", f"emitted on SUTANDO_DEBUG={env_val!r}")

    def test_unknown_top_level_keys_warn_on_load(self):
        _write_config(self.repo, "sutando.config.json", {
            "workspace": {"path": "/ws"},
            "vault": {"enabled": False},
            "workspce": "typo of workspace",  # noqa: SC2001 — intentional typo
        })
        import io
        buf = io.StringIO()
        saved_stderr = sys.stderr
        sys.stderr = buf
        try:
            cfg = load_config(repo_root=self.repo)
        finally:
            sys.stderr = saved_stderr
        # Loader keeps the unknown key in the parsed config (lenient policy),
        # but emits a one-line warning so the user sees the typo.
        self.assertIn("workspce", cfg)
        self.assertIn("workspce", buf.getvalue())
        self.assertIn("Known keys", buf.getvalue())

    def test_known_keys_only_does_not_warn(self):
        _write_config(self.repo, "sutando.config.json", {
            "workspace": {"path": "/ws"},
            "claude_sutando_config_dir": {"subdir": ".claude-sutando"},
            "vault": {"enabled": False},
        })
        import io
        buf = io.StringIO()
        saved_stderr = sys.stderr
        sys.stderr = buf
        try:
            load_config(repo_root=self.repo)
        finally:
            sys.stderr = saved_stderr
        # Regression guard: claude_sutando_config_dir must be in the known-keys
        # set, otherwise users adding the (documented) override block trigger
        # the misleading "unknown key — loader will ignore" warn even though
        # the loader DOES read it. Caught 2026-06-02 in commit 1 review.
        self.assertNotIn("does not read", buf.getvalue())
        self.assertNotIn("claude_sutando_config_dir", buf.getvalue())

    # ------------------------------------------------------------------ #
    #  10. core_config_dirs — v0.9 per-runtime env override surface       #
    # ------------------------------------------------------------------ #

    def test_core_config_dirs_synthesized_default_when_field_absent(self):
        # No `core_config_dirs` in config → loader synthesizes a single
        # claude-default entry so callers always get a usable list.
        _write_config(self.repo, "sutando.config.json", {})
        entries = resolve_core_config_dirs(repo_root=self.repo)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "claude-default")
        self.assertEqual(entries[0]["type"], "claude")
        self.assertEqual(entries[0]["env_name"], "CLAUDE_CONFIG_DIR")
        self.assertTrue(entries[0]["synced"])
        ws = resolve_workspace(repo_root=self.repo)
        self.assertEqual(entries[0]["value"], str(ws / ".claude-sutando"))

    def test_core_config_dirs_workspace_dir_token_expands(self):
        # ${WORKSPACE_DIR} should expand to the resolved workspace.
        _write_config(self.repo, "sutando.config.json", {
            "core_config_dirs": [{
                "id": "main",
                "type": "claude",
                "env_name": "CLAUDE_CONFIG_DIR",
                "synced": True,
                "value": "${WORKSPACE_DIR}/state/cc",
            }],
        })
        entries = resolve_core_config_dirs(repo_root=self.repo)
        ws = resolve_workspace(repo_root=self.repo)
        self.assertEqual(entries[0]["value"], str(ws / "state/cc"))

    def test_core_config_dirs_synced_true_rejects_outside_workspace(self):
        # synced=true means M2 sync must be able to track this tree. A value
        # outside the workspace would silently break that, so the loader
        # rejects with a clear error.
        _write_config(self.repo, "sutando.config.json", {
            "core_config_dirs": [{
                "id": "main",
                "type": "claude",
                "env_name": "CLAUDE_CONFIG_DIR",
                "synced": True,
                "value": "/etc/claude-state",  # outside the workspace
            }],
        })
        with self.assertRaises(ValueError) as cm:
            resolve_core_config_dirs(repo_root=self.repo)
        self.assertIn("synced=true", str(cm.exception))
        self.assertIn("not under workspace", str(cm.exception))

    def test_core_config_dirs_synced_false_allows_outside_workspace(self):
        # synced=false is the explicit opt-out — wrapper sets the env var,
        # user knows M2 sync skip this tree, no warning.
        _write_config(self.repo, "sutando.config.json", {
            "core_config_dirs": [{
                "id": "local-only",
                "type": "claude",
                "env_name": "CLAUDE_CONFIG_DIR",
                "synced": False,
                "value": "/etc/claude-state",
            }],
        })
        entries = resolve_core_config_dirs(repo_root=self.repo)
        self.assertEqual(entries[0]["value"], "/etc/claude-state")
        self.assertFalse(entries[0]["synced"])

    def test_core_config_dirs_duplicate_id_raises(self):
        _write_config(self.repo, "sutando.config.json", {
            "core_config_dirs": [
                {"id": "main", "type": "claude",
                 "env_name": "CLAUDE_CONFIG_DIR", "synced": True,
                 "value": "${WORKSPACE_DIR}/.claude-sutando"},
                {"id": "main", "type": "codex",
                 "env_name": "CODEX_CONFIG_DIR", "synced": False,
                 "value": "/tmp/codex"},
            ],
        })
        with self.assertRaises(ValueError) as cm:
            resolve_core_config_dirs(repo_root=self.repo)
        self.assertIn("duplicate id", str(cm.exception))

    def test_find_core_config_dir_picks_first_type_match(self):
        _write_config(self.repo, "sutando.config.json", {
            "core_config_dirs": [
                {"id": "alt", "type": "codex",
                 "env_name": "CODEX_CONFIG_DIR", "synced": False,
                 "value": "/tmp/codex"},
                {"id": "main", "type": "claude",
                 "env_name": "CLAUDE_CONFIG_DIR", "synced": True,
                 "value": "${WORKSPACE_DIR}/.claude-sutando"},
            ],
        })
        entry = find_core_config_dir(type_="claude", repo_root=self.repo)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], "main")
        # By-id selection independent of type:
        codex = find_core_config_dir(type_="claude", id_="alt", repo_root=self.repo)
        self.assertIsNotNone(codex)
        self.assertEqual(codex["env_name"], "CODEX_CONFIG_DIR")

    def test_resolve_claude_sutando_config_dir_prefers_core_config_dirs(self):
        # When the new schema is set (legacy field absent), the new schema
        # wins. The legacy field's deprecation path is exercised by a
        # different test. Both-set is now a hard-fail per Chi's directive
        # 2026-06-06 — covered by `test_raises_when_both_set` below.
        _write_config(self.repo, "sutando.config.json", {
            "core_config_dirs": [{
                "id": "main", "type": "claude",
                "env_name": "CLAUDE_CONFIG_DIR", "synced": True,
                "value": "${WORKSPACE_DIR}/state/new-path",
            }],
        })
        ccd = resolve_claude_sutando_config_dir(repo_root=self.repo)
        ws = resolve_workspace(repo_root=self.repo)
        self.assertEqual(str(ccd), str(ws / "state/new-path"))

    def test_resolve_claude_sutando_config_dir_legacy_subdir_still_honored(self):
        # When only the legacy field is set (no core_config_dirs), the loader
        # honors it for one release with a deprecation warning.
        _write_config(self.repo, "sutando.config.json", {
            "claude_sutando_config_dir": {"subdir": "my-legacy-claude"},
        })
        ccd = resolve_claude_sutando_config_dir(repo_root=self.repo)
        ws = resolve_workspace(repo_root=self.repo)
        # .resolve() because the legacy path goes through workspace / subdir
        # without ${WORKSPACE_DIR} expansion semantics.
        self.assertEqual(ccd, (ws / "my-legacy-claude").resolve())

    def test_resolve_claude_sutando_config_dir_raises_when_both_set(self):
        # Per Chi's directive 2026-06-06 on PR #1470: when BOTH the new
        # `core_config_dirs[type=claude]` AND legacy
        # `claude_sutando_config_dir.subdir` are set, that's a config error
        # and must hard-fail (NOT warn). Simultaneous presence means the
        # legacy block would be silently ignored — the user MUST be forced
        # to remove the dead config before the resolver returns a path.
        _write_config(self.repo, "sutando.config.json", {
            "core_config_dirs": [{
                "id": "main", "type": "claude",
                "env_name": "CLAUDE_CONFIG_DIR", "synced": True,
                "value": "${WORKSPACE_DIR}/state/new-path",
            }],
            "claude_sutando_config_dir": {"subdir": "legacy-subdir"},
        })
        with self.assertRaises(ValueError) as ctx:
            resolve_claude_sutando_config_dir(repo_root=self.repo)
        msg = str(ctx.exception)
        # Error must clearly name both fields + identify it as a config error
        # so the user can find what to remove.
        self.assertIn("both", msg.lower(),
                      f"expected 'both' in error; got: {msg!r}")
        self.assertIn("core_config_dirs", msg,
                      f"expected new field name in error; got: {msg!r}")
        self.assertIn("claude_sutando_config_dir", msg,
                      f"expected legacy field name in error; got: {msg!r}")
        self.assertIn("config error", msg.lower(),
                      f"expected 'config error' to surface the class; got: {msg!r}")

    def test_resolve_claude_sutando_config_dir_no_error_when_only_new_set(self):
        # Symmetric guard: when ONLY the new field is set (no legacy block),
        # resolution succeeds silently. Otherwise users on the clean new
        # path would hit the hard-fail erroneously.
        import io
        import contextlib

        _write_config(self.repo, "sutando.config.json", {
            "core_config_dirs": [{
                "id": "main", "type": "claude",
                "env_name": "CLAUDE_CONFIG_DIR", "synced": True,
                "value": "${WORKSPACE_DIR}/state/new-path",
            }],
            # NO claude_sutando_config_dir block at all
        })
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            ccd = resolve_claude_sutando_config_dir(repo_root=self.repo)
        ws = resolve_workspace(repo_root=self.repo)
        self.assertEqual(str(ccd), str(ws / "state/new-path"))
        # Stderr should be silent — no both-set warn, no legacy deprecation warn.
        stderr_text = stderr_buf.getvalue()
        self.assertEqual(stderr_text, "",
                         f"expected silent stderr on new-only path; got: {stderr_text!r}")

    # ------------------------------------------------------------------ #

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
