#!/usr/bin/env python3
"""Tests for check_memory_sync() vault.remote_url lookup in src/health-check.py.

Regression coverage for PR #1862 / issue #1450: the health check now reads
vault.remote_url from sutando.config.local.json (canonical) before falling
back to SUTANDO_MEMORY_REPO in .env (deprecated). The "never synced" message
points at sync-workspace.sh instead of sync-memory.sh.

Run: python3 tests/health-check-memory-sync-vault.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location(
    "health_check", REPO / "src" / "health-check.py"
)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class TestMemorySyncVaultLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hc-sync-vault-"))
        self._saved_repo = hc.REPO_DIR
        self._saved_ws = hc.WORKSPACE_DIR

    def tearDown(self):
        hc.REPO_DIR = self._saved_repo
        hc.WORKSPACE_DIR = self._saved_ws
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_repo(self, env_content=""):
        repo = self.tmp / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        if env_content:
            (repo / ".env").write_text(env_content)
        hc.REPO_DIR = repo
        ws = self.tmp / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        hc.WORKSPACE_DIR = ws
        return repo, ws

    def test_vault_remote_url_read_from_config(self):
        """vault.remote_url in sutando.config.local.json → no warn about not-configured."""
        repo, ws = self._set_repo()
        (repo / "sutando.config.local.json").write_text(
            json.dumps({"vault": {"enabled": True, "remote_url": "git@github.com:user/vault.git"}})
        )
        # Workspace is a git repo so freshness check runs.
        (ws / ".git").mkdir()
        with patch("sys.path", [str(REPO / "src")] + sys.path):
            result = hc.check_memory_sync()
        # Should not warn about missing config.
        self.assertNotIn("not configured", result.get("detail", ""))
        self.assertNotIn("SUTANDO_MEMORY_REPO", result.get("detail", ""))

    def test_env_fallback_when_vault_url_empty(self):
        """vault.remote_url empty → falls back to SUTANDO_MEMORY_REPO in .env."""
        repo, ws = self._set_repo(env_content='SUTANDO_MEMORY_REPO=git@github.com:user/vault.git\n')
        (repo / "sutando.config.local.json").write_text(json.dumps({"vault": {"remote_url": ""}}))
        (ws / ".git").mkdir()
        with patch("sys.path", [str(REPO / "src")] + sys.path):
            result = hc.check_memory_sync()
        self.assertNotIn("not configured", result.get("detail", ""))

    def test_no_config_no_env_is_single_machine_ok(self):
        """Neither vault.remote_url nor SUTANDO_MEMORY_REPO set → ok single-machine.

        RECONCILED with #2069: the original #1862 assertion here expected a
        `warn` mentioning vault.remote_url. #2069 (owner ask 2026-07-10)
        changed the not-configured verdict from warn → ok ("single-machine
        mode") to stop the recurring nag on single-machine installs. #2069 is
        the authoritative decision; the not-configured case is now an
        informational ok, still never mentioning the deprecated
        SUTANDO_MEMORY_REPO var.
        """
        repo, ws = self._set_repo()
        with patch("sys.path", [str(REPO / "src")] + sys.path):
            result = hc.check_memory_sync()
        self.assertEqual(result["status"], "ok")
        self.assertIn("single-machine", result["detail"])
        self.assertNotIn("SUTANDO_MEMORY_REPO", result["detail"])

    def test_never_synced_message_points_at_sync_workspace(self):
        """'Never synced' hint says sync-workspace.sh, not sync-memory.sh."""
        repo, ws = self._set_repo()
        (repo / "sutando.config.local.json").write_text(
            json.dumps({"vault": {"remote_url": "git@github.com:user/vault.git"}})
        )
        # No .git in workspace → falls through to legacy clone path checks
        # which both fail → "never synced" message.
        with patch("sys.path", [str(REPO / "src")] + sys.path):
            result = hc.check_memory_sync()
        detail = result.get("detail", "")
        self.assertNotIn("sync-memory.sh", detail)
        if "never synced" in detail:
            self.assertIn("sync-workspace.sh", detail)

    def test_freshness_detail_names_which_repo_it_measured(self):
        """The probe is called 'memory-sync' but the freshness signal it reads is
        the WORKSPACE vault fetch. A bare 'last sync Nh ago' reads as a claim about
        memory and contradicts a healthy memory sync, so the detail must say which.
        """
        repo, ws = self._set_repo()
        (repo / "sutando.config.local.json").write_text(
            json.dumps({"vault": {"remote_url": "git@github.com:user/vault.git"}})
        )
        git_dir = ws / ".git"
        git_dir.mkdir(parents=True, exist_ok=True)
        fetch_head = git_dir / "FETCH_HEAD"
        fetch_head.write_text("")
        stale = time.time() - (367 * 3600)
        os.utime(fetch_head, (stale, stale))

        with patch("sys.path", [str(REPO / "src")] + sys.path):
            result = hc.check_memory_sync()

        detail = result.get("detail", "")
        self.assertEqual(result["status"], "warn", f"367h should be stale: {result!r}")
        self.assertIn("stale", detail)
        self.assertIn("workspace", detail.lower(),
                      f"detail must name the workspace vault as its subject: {detail!r}")
        self.assertNotIn("sync-memory.sh", detail)


if __name__ == "__main__":
    unittest.main()
