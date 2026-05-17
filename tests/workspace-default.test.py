#!/usr/bin/env python3
"""Tests for src/workspace_default.py — workspace dir resolution contract.

Run: python3 tests/workspace-default.test.py
Exit: 0 on pass, 1 on fail.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import workspace_default  # noqa: E402
from workspace_default import default_workspace_dir, resolve_workspace  # noqa: E402


class TestWorkspaceDefault(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("SUTANDO_WORKSPACE")
        if "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_env
        elif "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]

    def test_default_is_dot_sutando_workspace_under_home(self):
        d = default_workspace_dir()
        self.assertEqual(d.name, "workspace")
        self.assertEqual(d.parent.name, ".sutando")
        self.assertEqual(d.parent.parent, Path.home())
        self.assertEqual(d, Path.home() / ".sutando" / "workspace")

    def test_resolve_uses_env_when_set(self):
        os.environ["SUTANDO_WORKSPACE"] = "/tmp/test-ws"
        # migrate=False to keep this test purely about resolution semantics.
        self.assertEqual(resolve_workspace(migrate=False), Path("/tmp/test-ws"))

    def test_resolve_expanduser_on_tilde(self):
        os.environ["SUTANDO_WORKSPACE"] = "~/custom-ws"
        self.assertEqual(resolve_workspace(migrate=False), Path.home() / "custom-ws")

    def test_resolve_falls_back_to_default_when_env_unset(self):
        self.assertEqual(resolve_workspace(migrate=False), default_workspace_dir())

    def test_resolve_falls_back_when_env_empty_string(self):
        os.environ["SUTANDO_WORKSPACE"] = ""
        self.assertEqual(resolve_workspace(migrate=False), default_workspace_dir())

    def test_resolve_falls_back_when_env_whitespace_only(self):
        os.environ["SUTANDO_WORKSPACE"] = "   "
        self.assertEqual(resolve_workspace(migrate=False), default_workspace_dir())

    def test_resolve_never_returns_repo_root(self):
        """Anti-regression: the historical fallback was the script's repo root
        (`Path(__file__).resolve().parent.parent`), which polluted git status
        with runtime artifacts. The default must NOT be a Sutando repo path."""
        d = resolve_workspace(migrate=False)
        self.assertNotEqual(d, ROOT)
        self.assertFalse(str(d).endswith("/sutando"))
        self.assertFalse(str(d).endswith("/sutando/"))


class TestMigrationFromLegacy(unittest.TestCase):
    """Tests for one-time auto-migration from legacy repo-root fallback to the
    new ~/.sutando/workspace/ default. Per sonichi's PR #762 review observation 1
    (https://github.com/sonichi/sutando/pull/762#review): Chi's MacBook node
    ran for 24+h with state in `~/Desktop/sutando/tasks/` via the repo-root
    fallback — after the default change, that state would be stranded unless
    migrated."""

    def setUp(self):
        self._saved_env = os.environ.get("SUTANDO_WORKSPACE")
        if "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]
        self.legacy = Path(tempfile.mkdtemp(prefix="ws-legacy-"))
        self.target = Path(tempfile.mkdtemp(prefix="ws-target-"))
        # New target should NOT exist (or be empty) for migration to fire;
        # the mkdtemp created an empty dir, which satisfies "exists but empty"
        # — the function bails if target has content, but an empty target
        # is fine. Remove the dir so migrate creates it cleanly.
        self.target.rmdir()

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_env
        elif "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]
        shutil.rmtree(self.legacy, ignore_errors=True)
        shutil.rmtree(self.target, ignore_errors=True)

    def _seed_legacy_runtime(self):
        """Populate legacy/{tasks,results,state} with task-* files so the
        migration trigger fires."""
        (self.legacy / "tasks").mkdir(parents=True)
        (self.legacy / "tasks" / "task-1.txt").write_text("legacy task 1")
        (self.legacy / "tasks" / "task-2.txt").write_text("legacy task 2")
        (self.legacy / "results").mkdir(parents=True)
        (self.legacy / "results" / "task-1.txt").write_text("legacy result")
        (self.legacy / "state").mkdir(parents=True)
        (self.legacy / "state" / "some-state.json").write_text("{}")

    def test_migration_moves_runtime_dirs_when_legacy_has_task_files(self):
        self._seed_legacy_runtime()
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.legacy), \
             patch.object(workspace_default, "default_workspace_dir", return_value=self.target):
            moved = workspace_default._migrate_from_legacy(self.target)
        self.assertTrue(moved)
        # All three dirs should now exist under target.
        self.assertTrue((self.target / "tasks").is_dir())
        self.assertTrue((self.target / "results").is_dir())
        self.assertTrue((self.target / "state").is_dir())
        # Contents preserved.
        self.assertEqual((self.target / "tasks" / "task-1.txt").read_text(), "legacy task 1")
        self.assertEqual((self.target / "state" / "some-state.json").read_text(), "{}")
        # Legacy side is gone (we moved, not copied).
        self.assertFalse((self.legacy / "tasks").exists())
        self.assertFalse((self.legacy / "results").exists())
        self.assertFalse((self.legacy / "state").exists())

    def test_migration_skips_when_legacy_has_no_task_files(self):
        # Legacy exists but no task-* anywhere — could be a fresh checkout.
        (self.legacy / "src").mkdir()
        (self.legacy / "src" / "something.py").write_text("")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.legacy):
            moved = workspace_default._migrate_from_legacy(self.target)
        self.assertFalse(moved)
        self.assertFalse(self.target.exists() or self.target.is_dir())

    def test_migration_skips_when_target_has_content(self):
        # User already has state at the new path — don't clobber.
        self._seed_legacy_runtime()
        self.target.mkdir(parents=True)
        (self.target / "tasks").mkdir()
        (self.target / "tasks" / "existing-task.txt").write_text("DO NOT TOUCH")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.legacy):
            moved = workspace_default._migrate_from_legacy(self.target)
        self.assertFalse(moved)
        # Legacy data untouched (because target had content).
        self.assertTrue((self.legacy / "tasks" / "task-1.txt").exists())
        # Target untouched.
        self.assertEqual((self.target / "tasks" / "existing-task.txt").read_text(), "DO NOT TOUCH")

    def test_migration_idempotent_on_second_run(self):
        self._seed_legacy_runtime()
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.legacy):
            moved_1 = workspace_default._migrate_from_legacy(self.target)
            moved_2 = workspace_default._migrate_from_legacy(self.target)
        self.assertTrue(moved_1)
        self.assertFalse(moved_2)  # second run finds target populated, bails

    def test_resolve_workspace_skips_migration_when_env_set(self):
        """When SUTANDO_WORKSPACE is set, migrate=True still skips the legacy
        path entirely (env-pinned users opt out of any auto-mv)."""
        self._seed_legacy_runtime()
        os.environ["SUTANDO_WORKSPACE"] = str(self.target)
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.legacy):
            ws = resolve_workspace(migrate=True)
        self.assertEqual(ws, self.target)
        # Legacy state should be UNTOUCHED — env pin bypasses migration.
        self.assertTrue((self.legacy / "tasks" / "task-1.txt").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
