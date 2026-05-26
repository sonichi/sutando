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
from workspace_default import (  # noqa: E402
    default_workspace_dir,
    resolve_workspace,
    status_path,
    status_read_path,
)


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

    def test_migration_skips_when_legacy_has_no_runtime_files(self):
        # Legacy exists but only has code, no runtime state — fresh checkout.
        (self.legacy / "src").mkdir()
        (self.legacy / "src" / "something.py").write_text("")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.legacy):
            moved = workspace_default._migrate_from_legacy(self.target)
        self.assertFalse(moved)
        self.assertFalse(self.target.exists() or self.target.is_dir())

    def test_migration_triggers_for_observer_node_with_results_only(self):
        # Observer-only node: no tasks/ written, but results/ is populated.
        # Previously missed because the check was task-* only (issue #770).
        (self.legacy / "results").mkdir(parents=True)
        (self.legacy / "results" / "some-result.txt").write_text("result data")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.legacy), \
             patch.object(workspace_default, "default_workspace_dir", return_value=self.target):
            moved = workspace_default._migrate_from_legacy(self.target)
        self.assertTrue(moved)
        self.assertTrue((self.target / "results" / "some-result.txt").exists())
        self.assertFalse((self.legacy / "results").exists())

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

    def test_resolve_workspace_skips_legacy_migration_when_env_set(self):
        """When SUTANDO_WORKSPACE is set, the LEGACY migration (tasks/results/state)
        is skipped. The notes in-repo migration runs independently — see the
        separate test class for that."""
        self._seed_legacy_runtime()
        os.environ["SUTANDO_WORKSPACE"] = str(self.target)
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.legacy):
            ws = resolve_workspace(migrate=True)
        self.assertEqual(ws, self.target)
        # Legacy state should be UNTOUCHED — env pin bypasses _migrate_from_legacy.
        self.assertTrue((self.legacy / "tasks" / "task-1.txt").exists())


class TestInRepoNotesMigration(unittest.TestCase):
    """Tests for `_migrate_inrepo_notes` — the env-set migration that catches
    stragglers in `<repo>/notes/` after the workspace contract moved canonical
    notes to `<workspace>/notes/`.

    Different trigger than `_migrate_from_legacy`: that one is for env-UNSET
    legacy installs. This one is for env-SET installs whose code was writing
    to in-repo notes/ until the path-fix PR landed."""

    def setUp(self):
        self._saved_env = os.environ.get("SUTANDO_WORKSPACE")
        self.repo_root = Path(tempfile.mkdtemp(prefix="ws-repo-"))
        self.workspace = Path(tempfile.mkdtemp(prefix="ws-target-"))
        # Provide env-set workspace pointing at our fixture.
        os.environ["SUTANDO_WORKSPACE"] = str(self.workspace)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_env
        elif "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]
        shutil.rmtree(self.repo_root, ignore_errors=True)
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _seed_inrepo_notes(self, *names):
        (self.repo_root / "notes").mkdir(parents=True, exist_ok=True)
        for name in names:
            (self.repo_root / "notes" / name).write_text(f"content of {name}")

    def test_inrepo_notes_migrate_when_workspace_differs(self):
        self._seed_inrepo_notes("a.md", "b.md")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            moved = workspace_default._migrate_inrepo_notes(self.workspace)
        self.assertTrue(moved)
        self.assertTrue((self.workspace / "notes" / "a.md").is_file())
        self.assertTrue((self.workspace / "notes" / "b.md").is_file())
        # Source side should be drained.
        self.assertFalse((self.repo_root / "notes" / "a.md").exists())
        self.assertFalse((self.repo_root / "notes" / "b.md").exists())

    def test_inrepo_notes_skip_when_repo_equals_workspace(self):
        # If workspace IS the repo root, the function bails (no infinite mv).
        self._seed_inrepo_notes("x.md")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            moved = workspace_default._migrate_inrepo_notes(self.repo_root)
        self.assertFalse(moved)
        # File should still be where it was.
        self.assertTrue((self.repo_root / "notes" / "x.md").exists())

    def test_inrepo_notes_skip_when_inrepo_notes_missing(self):
        # Fresh checkout — no in-repo notes/ dir.
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            moved = workspace_default._migrate_inrepo_notes(self.workspace)
        self.assertFalse(moved)

    def test_inrepo_notes_no_clobber_on_collision(self):
        self._seed_inrepo_notes("clash.md")
        (self.workspace / "notes").mkdir(parents=True)
        (self.workspace / "notes" / "clash.md").write_text("WORKSPACE WINS")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            workspace_default._migrate_inrepo_notes(self.workspace)
        # Workspace file untouched, in-repo file still present (skipped, not moved).
        self.assertEqual((self.workspace / "notes" / "clash.md").read_text(), "WORKSPACE WINS")
        self.assertTrue((self.repo_root / "notes" / "clash.md").exists())

    def test_inrepo_notes_ignores_subdirs_and_non_text(self):
        # Subdir and unusual extension — both left alone.
        (self.repo_root / "notes" / "media").mkdir(parents=True)
        (self.repo_root / "notes" / "media" / "video.mp4").write_text("dummy")
        (self.repo_root / "notes" / "scratch.bin").write_text("opaque")
        self._seed_inrepo_notes("real-note.md")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            workspace_default._migrate_inrepo_notes(self.workspace)
        # Only the .md was moved.
        self.assertTrue((self.workspace / "notes" / "real-note.md").exists())
        self.assertFalse((self.workspace / "notes" / "scratch.bin").exists())
        self.assertFalse((self.workspace / "notes" / "video.mp4").exists())
        self.assertTrue((self.repo_root / "notes" / "media" / "video.mp4").exists())
        self.assertTrue((self.repo_root / "notes" / "scratch.bin").exists())

    def test_inrepo_notes_idempotent(self):
        self._seed_inrepo_notes("once.md")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            moved_1 = workspace_default._migrate_inrepo_notes(self.workspace)
            moved_2 = workspace_default._migrate_inrepo_notes(self.workspace)
        self.assertTrue(moved_1)
        self.assertFalse(moved_2)  # second run finds in-repo notes/ empty (file moved)

    @unittest.skip(
        "#1169: auto-migration disabled from resolve_workspace(). "
        "_migrate_inrepo_notes function itself is still tested directly "
        "above; the auto-dispatch is now opt-in via the sutando-migrate CLI "
        "(follow-up PR)."
    )
    def test_resolve_workspace_runs_inrepo_notes_migration_when_env_set(self):
        # End-to-end: resolve_workspace called with env-set workspace, in-repo
        # has notes, migration runs on the way through.
        self._seed_inrepo_notes("e2e.md")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            ws = resolve_workspace(migrate=True)
        self.assertEqual(ws, self.workspace)
        self.assertTrue((self.workspace / "notes" / "e2e.md").exists())
        self.assertFalse((self.repo_root / "notes" / "e2e.md").exists())


class TestInRepoBuildLogMigration(unittest.TestCase):
    """Tests for `_migrate_inrepo_build_log` — moves build_log.md from repo
    root to workspace (parallel to _migrate_inrepo_notes but single-file)."""

    def setUp(self):
        self._saved_env = os.environ.get("SUTANDO_WORKSPACE")
        self.repo_root = Path(tempfile.mkdtemp(prefix="ws-repo-"))
        self.workspace = Path(tempfile.mkdtemp(prefix="ws-target-"))
        os.environ["SUTANDO_WORKSPACE"] = str(self.workspace)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_env
        elif "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]
        shutil.rmtree(self.repo_root, ignore_errors=True)
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_migrate_when_inrepo_exists_and_workspace_differs(self):
        (self.repo_root / "build_log.md").write_text("# Old log\n\nLog content.\n")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            moved = workspace_default._migrate_inrepo_build_log(self.workspace)
        self.assertTrue(moved)
        self.assertTrue((self.workspace / "build_log.md").is_file())
        self.assertEqual(
            (self.workspace / "build_log.md").read_text(),
            "# Old log\n\nLog content.\n",
        )
        self.assertFalse((self.repo_root / "build_log.md").exists())

    def test_skip_when_workspace_equals_repo(self):
        (self.repo_root / "build_log.md").write_text("dont touch")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            moved = workspace_default._migrate_inrepo_build_log(self.repo_root)
        self.assertFalse(moved)
        self.assertTrue((self.repo_root / "build_log.md").exists())

    def test_skip_when_inrepo_missing(self):
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            moved = workspace_default._migrate_inrepo_build_log(self.workspace)
        self.assertFalse(moved)

    def test_no_clobber_on_collision(self):
        (self.repo_root / "build_log.md").write_text("REPO version")
        (self.workspace).mkdir(parents=True, exist_ok=True)
        (self.workspace / "build_log.md").write_text("WORKSPACE wins")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            workspace_default._migrate_inrepo_build_log(self.workspace)
        # Workspace untouched, in-repo file still present (skipped, not moved).
        self.assertEqual((self.workspace / "build_log.md").read_text(), "WORKSPACE wins")
        self.assertTrue((self.repo_root / "build_log.md").exists())

    def test_idempotent_via_sentinel(self):
        (self.repo_root / "build_log.md").write_text("once")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            moved_1 = workspace_default._migrate_inrepo_build_log(self.workspace)
            # Re-seed the in-repo file so we can verify the sentinel (not the
            # missing-file early-return) is what prevents re-migration.
            (self.repo_root / "build_log.md").write_text("twice")
            moved_2 = workspace_default._migrate_inrepo_build_log(self.workspace)
        self.assertTrue(moved_1)
        self.assertFalse(moved_2)
        # Workspace still has the FIRST content (second run skipped via sentinel).
        self.assertEqual((self.workspace / "build_log.md").read_text(), "once")

    @unittest.skip(
        "#1169: auto-migration disabled from resolve_workspace(). "
        "_migrate_inrepo_build_log function itself is still tested directly "
        "above; the auto-dispatch is now opt-in via the sutando-migrate CLI "
        "(follow-up PR)."
    )
    def test_resolve_workspace_runs_build_log_migration_when_env_set(self):
        (self.repo_root / "build_log.md").write_text("e2e content")
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.repo_root):
            ws = resolve_workspace(migrate=True)
        self.assertEqual(ws, self.workspace)
        self.assertTrue((self.workspace / "build_log.md").exists())
        self.assertFalse((self.repo_root / "build_log.md").exists())

    @unittest.skip(
        "#1169: auto-migration disabled from resolve_workspace(). "
        "_migrate_from_legacy + _migrate_inrepo_build_log are still tested "
        "directly above; the env-unset auto-dispatch is now opt-in via "
        "sutando-migrate CLI (follow-up PR)."
    )
    def test_legacy_install_env_unset_build_log_migrates_after_dirs(self):
        """Corner case Mini flagged in PR #859 review:
          - Legacy install: repo has tasks/, results/, state/ with content
          - env SUTANDO_WORKSPACE unset
          - New default doesn't yet exist
          - build_log.md ALSO at repo root

        Expected sequence inside resolve_workspace():
          1. _migrate_from_legacy fires (env unset + target absent + runtime
             evidence) → moves tasks/results/state to target.
          2. _migrate_inrepo_build_log fires after (env-unset path also calls it,
             per v4 of _migrate_inrepo_build_log) → sees workspace now exists,
             workspace has no build_log.md yet, repo has one → migrates it.

        Mini's concern was that step-2 might "skip (collision)" because step-1
        created the target dir. This test pins the actual behavior: build_log
        DOES migrate (target check is against the FILE workspace/build_log.md,
        not the dir).
        """
        # Drop env so the unset-branch of resolve_workspace executes.
        self._saved_env = os.environ.pop("SUTANDO_WORKSPACE", None)
        try:
            legacy = Path(tempfile.mkdtemp(prefix="ws-mini-nit-legacy-"))
            target = Path(tempfile.mkdtemp(prefix="ws-mini-nit-target-"))
            target.rmdir()  # _migrate_from_legacy bails if target exists+nonempty
            # Seed legacy: runtime dirs with content + build_log at root.
            (legacy / "tasks").mkdir()
            (legacy / "tasks" / "task-1.txt").write_text("legacy task")
            (legacy / "results").mkdir()
            (legacy / "results" / "task-1.txt").write_text("legacy result")
            (legacy / "state").mkdir()
            (legacy / "state" / "x.json").write_text("{}")
            (legacy / "build_log.md").write_text("# legacy build log\nimportant\n")
            with patch.object(workspace_default, "_legacy_repo_root", return_value=legacy), \
                 patch.object(workspace_default, "default_workspace_dir", return_value=target):
                ws = resolve_workspace(migrate=True)
            self.assertEqual(ws, target)
            # Runtime dirs landed at target.
            self.assertTrue((target / "tasks" / "task-1.txt").exists())
            self.assertTrue((target / "results" / "task-1.txt").exists())
            self.assertTrue((target / "state" / "x.json").exists())
            # AND build_log.md ALSO migrated — not stranded.
            self.assertTrue((target / "build_log.md").is_file())
            self.assertEqual(
                (target / "build_log.md").read_text(),
                "# legacy build log\nimportant\n",
            )
            self.assertFalse((legacy / "build_log.md").exists())
        finally:
            shutil.rmtree(legacy, ignore_errors=True)
            shutil.rmtree(target, ignore_errors=True)


class TestStatusPathHelpers(unittest.TestCase):
    """Tests for `status_path` (write location) and `status_read_path`
    (read location with one-release legacy-root fallback)."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="ws-status-"))

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_status_path_is_under_state(self):
        p = status_path("core-status.json", self.workspace)
        self.assertEqual(p, self.workspace / "state" / "core-status.json")

    def test_read_path_prefers_state(self):
        (self.workspace / "state").mkdir(parents=True)
        (self.workspace / "state" / "core-status.json").write_text("{}")
        (self.workspace / "core-status.json").write_text("{}")  # legacy too
        self.assertEqual(
            status_read_path("core-status.json", self.workspace),
            self.workspace / "state" / "core-status.json",
        )

    def test_read_path_falls_back_to_legacy_root(self):
        (self.workspace / "core-status.json").write_text("{}")
        self.assertEqual(
            status_read_path("core-status.json", self.workspace),
            self.workspace / "core-status.json",
        )

    def test_read_path_returns_state_path_when_neither_exists(self):
        # Caller handles the missing case; we always point at the canonical home.
        self.assertEqual(
            status_read_path("core-status.json", self.workspace),
            self.workspace / "state" / "core-status.json",
        )


class TestRootStatusMigration(unittest.TestCase):
    """Tests for `_migrate_root_status` — sweeps loose workspace-root status
    .json files into `state/`. Parallel posture to `_migrate_inrepo_build_log`:
    non-destructive on collision, sentinel-gated."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="ws-rootstatus-"))

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_moves_root_status_files_into_state(self):
        (self.workspace / "core-status.json").write_text('{"status":"idle"}')
        (self.workspace / "voice-state.json").write_text('{"connected":false}')
        moved = workspace_default._migrate_root_status(self.workspace)
        self.assertTrue(moved)
        self.assertEqual(
            (self.workspace / "state" / "core-status.json").read_text(),
            '{"status":"idle"}',
        )
        self.assertTrue((self.workspace / "state" / "voice-state.json").is_file())
        # Root copies are gone (moved, not copied).
        self.assertFalse((self.workspace / "core-status.json").exists())
        self.assertFalse((self.workspace / "voice-state.json").exists())

    def test_no_clobber_on_collision(self):
        (self.workspace / "core-status.json").write_text("ROOT version")
        (self.workspace / "state").mkdir()
        (self.workspace / "state" / "core-status.json").write_text("STATE wins")
        workspace_default._migrate_root_status(self.workspace)
        # state/ copy untouched, root copy left in place (skipped, not moved).
        self.assertEqual(
            (self.workspace / "state" / "core-status.json").read_text(), "STATE wins"
        )
        self.assertTrue((self.workspace / "core-status.json").exists())

    def test_idempotent_via_sentinel(self):
        (self.workspace / "core-status.json").write_text("once")
        moved_1 = workspace_default._migrate_root_status(self.workspace)
        # Re-seed a root file; the sentinel (not a missing file) must block it.
        (self.workspace / "voice-state.json").write_text("twice")
        moved_2 = workspace_default._migrate_root_status(self.workspace)
        self.assertTrue(moved_1)
        self.assertFalse(moved_2)
        self.assertTrue((self.workspace / ".status-migrated").exists())
        # Second file stayed at root because the sentinel short-circuited.
        self.assertTrue((self.workspace / "voice-state.json").exists())

    def test_writes_sentinel_even_when_nothing_to_move(self):
        moved = workspace_default._migrate_root_status(self.workspace)
        self.assertFalse(moved)
        self.assertTrue((self.workspace / ".status-migrated").exists())


class TestConversationLogMigration(unittest.TestCase):
    """Tests for `_migrate_conversation_log` — moves conversation.log from the
    workspace root into `logs/` (it's a transcript, not a status file)."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="ws-convlog-"))

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_moves_conversation_log_into_logs(self):
        (self.workspace / "conversation.log").write_text("turn 1\nturn 2\n")
        moved = workspace_default._migrate_conversation_log(self.workspace)
        self.assertTrue(moved)
        self.assertEqual(
            (self.workspace / "logs" / "conversation.log").read_text(),
            "turn 1\nturn 2\n",
        )
        self.assertFalse((self.workspace / "conversation.log").exists())

    def test_no_clobber_on_collision(self):
        (self.workspace / "conversation.log").write_text("ROOT log")
        (self.workspace / "logs").mkdir()
        (self.workspace / "logs" / "conversation.log").write_text("LOGS wins")
        workspace_default._migrate_conversation_log(self.workspace)
        self.assertEqual(
            (self.workspace / "logs" / "conversation.log").read_text(), "LOGS wins"
        )

    def test_idempotent_via_sentinel(self):
        (self.workspace / "conversation.log").write_text("once")
        moved_1 = workspace_default._migrate_conversation_log(self.workspace)
        (self.workspace / "conversation.log").write_text("twice")
        moved_2 = workspace_default._migrate_conversation_log(self.workspace)
        self.assertTrue(moved_1)
        self.assertFalse(moved_2)
        self.assertTrue((self.workspace / ".conversation-log-migrated").exists())


class TestResolveWorkspaceRunsNewMigrators(unittest.TestCase):
    """End-to-end: resolve_workspace sweeps root status files + conversation.log
    on the way through, for both the env-set and default branches."""

    def setUp(self):
        self._saved_env = os.environ.get("SUTANDO_WORKSPACE")
        self.workspace = Path(tempfile.mkdtemp(prefix="ws-e2e-"))
        os.environ["SUTANDO_WORKSPACE"] = str(self.workspace)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_env
        elif "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]
        shutil.rmtree(self.workspace, ignore_errors=True)

    @unittest.skip(
        "#1169: auto-migration disabled from resolve_workspace(). "
        "_migrate_root_status + _migrate_conversation_log are still tested "
        "directly above; the auto-dispatch is now opt-in via the "
        "sutando-migrate CLI (follow-up PR)."
    )
    def test_env_set_resolve_runs_status_and_convlog_migration(self):
        (self.workspace / "core-status.json").write_text('{"status":"idle"}')
        (self.workspace / "conversation.log").write_text("a turn\n")
        # Avoid the in-repo notes/build_log migrators touching the real repo.
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.workspace):
            ws = resolve_workspace(migrate=True)
        self.assertEqual(ws, self.workspace)
        self.assertTrue((self.workspace / "state" / "core-status.json").is_file())
        self.assertFalse((self.workspace / "core-status.json").exists())
        self.assertTrue((self.workspace / "logs" / "conversation.log").is_file())
        self.assertFalse((self.workspace / "conversation.log").exists())


class TestPostMigrationDisableBehavior(unittest.TestCase):
    """Positive assertions on the post-#1169 contract: resolve_workspace()
    leaves legacy sources untouched, emits the notice once when called with
    migrate=True, and stays fully pure (no scan, no stderr) with migrate=False.

    These replace the auto-dispatch tests above. The skipped ones are kept
    for one release as ratchet documentation; this class is the canonical
    assertion of the new behavior."""

    def setUp(self):
        self._saved_env = os.environ.get("SUTANDO_WORKSPACE")
        if "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]
        self.tmpdir = tempfile.mkdtemp()
        self.workspace = Path(self.tmpdir) / "ws"
        self.workspace.mkdir()
        self.legacy = Path(self.tmpdir) / "legacy"
        self.legacy.mkdir()
        (self.legacy / "notes").mkdir()
        (self.legacy / "notes" / "x.md").write_text("a real note\n")
        (self.legacy / "build_log.md").write_text("# build log\n")
        (self.legacy / "conversation.log").write_text("a turn\n")
        os.environ["SUTANDO_WORKSPACE"] = str(self.workspace)
        # Reset the module-level notice guard between tests so each can
        # observe the once-per-process behavior independently.
        workspace_default._AUTO_MIGRATE_NOTICE_PRINTED = False

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_env
        elif "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        workspace_default._AUTO_MIGRATE_NOTICE_PRINTED = False

    def test_legacy_sources_untouched_with_migrate_true(self):
        """resolve_workspace(migrate=True) must NOT move any legacy file."""
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.legacy):
            ws = resolve_workspace(migrate=True)
        self.assertEqual(ws, self.workspace)
        # Legacy files unchanged
        self.assertTrue((self.legacy / "notes" / "x.md").is_file())
        self.assertTrue((self.legacy / "build_log.md").is_file())
        self.assertTrue((self.legacy / "conversation.log").is_file())
        # And nothing was moved INTO the workspace
        self.assertFalse((self.workspace / "notes").exists())
        self.assertFalse((self.workspace / "build_log.md").exists())
        self.assertFalse((self.workspace / "logs" / "conversation.log").exists())

    def test_notice_fires_once_then_silences(self):
        """The legacy-state stderr notice must fire exactly once per process."""
        from io import StringIO
        with patch.object(workspace_default, "_legacy_repo_root", return_value=self.legacy):
            buf = StringIO()
            with patch.object(sys, "stderr", buf):
                resolve_workspace(migrate=True)
            first = buf.getvalue()
            buf = StringIO()
            with patch.object(sys, "stderr", buf):
                resolve_workspace(migrate=True)
            second = buf.getvalue()
        self.assertIn("legacy state detected", first)
        self.assertIn("#1169", first)
        self.assertIn("sutando-migrate.sh", first)
        self.assertEqual("", second, "notice fired more than once")

    def test_migrate_false_stays_pure(self):
        """migrate=False must skip the scan + stderr entirely. No I/O on legacy."""
        from io import StringIO
        with patch.object(workspace_default, "_legacy_repo_root") as mock_repo:
            buf = StringIO()
            with patch.object(sys, "stderr", buf):
                ws = resolve_workspace(migrate=False)
            self.assertEqual(ws, self.workspace)
            # _legacy_repo_root() must NOT be called when migrate=False
            mock_repo.assert_not_called()
            # No stderr output
            self.assertEqual("", buf.getvalue())
        # Legacy files unchanged
        self.assertTrue((self.legacy / "notes" / "x.md").is_file())

    def test_no_notice_when_no_legacy_state(self):
        """Clean install: notice must not fire when there's nothing to migrate."""
        from io import StringIO
        empty = Path(self.tmpdir) / "empty"
        empty.mkdir()
        with patch.object(workspace_default, "_legacy_repo_root", return_value=empty):
            buf = StringIO()
            with patch.object(sys, "stderr", buf):
                resolve_workspace(migrate=True)
            self.assertEqual("", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
