#!/usr/bin/env python3
"""Tests for check_memory_sync()'s opt-out vs never-configured distinction (#2231).

`_resolved_vault()` used to derive `_explicit_disable` from `load_config()`,
which deep-merges the repo-shipped `sutando.config.json`. That file ships
`vault.enabled: false` to every clone, so the flag was True on every host that
had simply never configured a vault — making check_memory_sync always report
"config opt-out" and leaving its "not configured (single-machine mode)" branch
unreachable.

CRITICAL FIXTURE NOTE: every test here writes the shipped `sutando.config.json`
into the temp repo. Without it, `load_config()` finds no repo root, returns {},
and `_explicit_disable` is False for the *wrong* reason — the fixture would pass
against the buggy code and prove nothing. The sibling
`health-check-memory-sync-vault.test.py` omits that file, which is exactly why
this bug survived.

Run: python3 tests/health-check-memory-sync-optout.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location(
    "health_check", REPO / "src" / "health-check.py"
)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

# Mirrors the real repo-shipped defaults: sync is opt-in, so the committed file
# carries enabled=false for everyone.
SHIPPED_DEFAULTS = {"vault": {"enabled": False, "remote_url": ""}}


class TestOptOutVsNeverConfigured(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hc-optout-"))
        self._saved_repo = hc.REPO_DIR
        self._saved_ws = hc.WORKSPACE_DIR
        self.repo = self.tmp / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        # The shipped defaults — present on every real clone.
        (self.repo / "sutando.config.json").write_text(json.dumps(SHIPPED_DEFAULTS))
        hc.REPO_DIR = self.repo
        ws = self.tmp / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        hc.WORKSPACE_DIR = ws
        self._clear_config_cache()

    def tearDown(self):
        hc.REPO_DIR = self._saved_repo
        hc.WORKSPACE_DIR = self._saved_ws
        self._clear_config_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _clear_config_cache():
        """sutando_config memoizes load_config; stale cache would cross tests."""
        try:
            import sutando_config
            sutando_config._CACHE = None
            sutando_config._CACHE_REPO_ROOT = None
        except Exception:
            pass

    def _write_local(self, cfg):
        (self.repo / "sutando.config.local.json").write_text(json.dumps(cfg))
        self._clear_config_cache()

    # -- the regression ----------------------------------------------------- #

    def test_no_local_config_is_not_an_opt_out(self):
        """No override file: the host chose nothing, despite shipped enabled=false.

        This is the #2231 regression. Pre-fix, _explicit_disable was True here.
        """
        vault = hc._resolved_vault()
        self.assertFalse(
            vault["_explicit_disable"],
            "a host with no sutando.config.local.json never opted out",
        )
        result = hc.check_memory_sync()
        self.assertIn("not configured", result["detail"])
        self.assertNotIn("opt-out", result["detail"])

    def test_local_config_without_vault_block_is_not_an_opt_out(self):
        """An override file that says nothing about vault is not a choice either."""
        self._write_local({"workspace": {"path": "/tmp/ws"}})
        self.assertFalse(hc._resolved_vault()["_explicit_disable"])
        self.assertIn("not configured", hc.check_memory_sync()["detail"])

    # -- the branch that must still work ------------------------------------ #

    def test_local_enabled_false_is_a_real_opt_out(self):
        """Explicit vault.enabled=false in the override IS a deliberate opt-out."""
        self._write_local({"vault": {"enabled": False}})
        self.assertTrue(hc._resolved_vault()["_explicit_disable"])
        self.assertIn("opt-out", hc.check_memory_sync()["detail"])

    def test_local_enabled_true_is_not_an_opt_out(self):
        self._write_local({"vault": {"enabled": True, "remote_url": "git@x:y/z.git"}})
        self.assertFalse(hc._resolved_vault()["_explicit_disable"])

    # -- defensive branches of the helper itself --------------------------- #
    #
    # These call _local_vault_enabled_is_false() DIRECTLY, not through
    # _resolved_vault(). That is deliberate and load-bearing: _resolved_vault()
    # runs resolve_vault()/load_config() FIRST, and load_config() raises on a
    # malformed config — so a malformed-file test routed through _resolved_vault
    # is caught by its OUTER except and never reaches the helper's own
    # `except (OSError, ValueError)`. The assertion (returns False) passes
    # either way; only line-coverage reveals the branch was never run. The
    # coverage gate caught exactly that on the first push (missing 146-147).

    def test_malformed_local_config_returns_false(self):
        """Unparseable override → helper's own except branch → False."""
        (self.repo / "sutando.config.local.json").write_text("{not json")
        self.assertFalse(hc._local_vault_enabled_is_false())

    def test_no_local_file_returns_false(self):
        """Absent override → early `not is_file()` return, no read attempted."""
        self.assertFalse((self.repo / "sutando.config.local.json").exists())
        self.assertFalse(hc._local_vault_enabled_is_false())

    def test_helper_reads_authored_false_directly(self):
        """Authored enabled=false → True, without going through _resolved_vault."""
        self._write_local({"vault": {"enabled": False}})
        self.assertTrue(hc._local_vault_enabled_is_false())

    # -- value semantics (via the public path) ----------------------------- #

    def test_vault_key_present_but_null(self):
        """`"vault": null` must not raise and must not read as opt-out."""
        self._write_local({"vault": None})
        self.assertFalse(hc._resolved_vault()["_explicit_disable"])

    def test_enabled_absent_from_vault_block(self):
        """A vault block without `enabled` is not an opt-out (is-False, not falsy)."""
        self._write_local({"vault": {"remote_url": ""}})
        self.assertFalse(hc._resolved_vault()["_explicit_disable"])

    def test_enabled_zero_is_not_false(self):
        """`0` is falsy but is not an explicit disable — `is False` matters."""
        self._write_local({"vault": {"enabled": 0}})
        self.assertFalse(
            hc._resolved_vault()["_explicit_disable"],
            "0 is not the boolean False; only an authored `false` counts",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
