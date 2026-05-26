#!/usr/bin/env python3
"""Integration tests for `src/workspace_default.py`.

This module is 305 LOC of path resolution + three one-time auto-
migrations (`_migrate_from_legacy`, `_migrate_inrepo_notes`,
`_migrate_inrepo_build_log`). It had zero tests; recent fix history
referenced multiple migration incidents (stranded owner DMs from
bridge-vs-bundle CWD mismatch, in-repo notes splitting between
locations, build_log living in two places at once). Exactly the kind
of code where silent bugs hide.

Tests are scoped to behaviors that probe failure modes:

  - **Relative `SUTANDO_WORKSPACE` is anchored to absolute.** This PR's
    bug fix. A relative env path would otherwise resolve against CWD,
    leading to split-brain when launchd-managed and bare-shell
    processes inherit the same env but run with different CWDs.
  - **Legacy migration moves runtime-state dirs and is idempotent.**
    The function should fire once, populate the workspace, and bail on
    repeat invocations.
  - **In-repo notes migration is flat-only.** The flat-notes convention
    is enforced by skipping subdirectories — pin it so a future
    "recursive=True by default" change is deliberate, not silent.
  - **Build_log migration is non-destructive on collision.** When the
    workspace already has a build_log.md, the repo copy must NOT
    clobber it (the workspace copy is authoritative once it exists).

`_legacy_repo_root` is monkeypatched per case so we don't drag the
real repo state into the test.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ws = _load("ws_default", REPO / "src" / "workspace_default.py")


def _fresh_layout():
    """Make a fresh temp layout: ``<tmp>/repo`` (the fake legacy repo)
    and return its path. Caller uses ``<tmp>/workspace`` as the
    workspace target. Both start empty."""
    tmp = Path(tempfile.mkdtemp(prefix="sutando-ws-test-"))
    repo = tmp / "repo"
    repo.mkdir()
    return tmp, repo


def _override_legacy(repo_path: Path):
    """Monkeypatch `_legacy_repo_root` to return ``repo_path`` and
    return a restore handle."""
    original = ws._legacy_repo_root
    ws._legacy_repo_root = lambda: repo_path
    return original


def _restore_legacy(original):
    ws._legacy_repo_root = original


def _clear_env():
    """Capture and clear env vars that affect resolution. Returns a
    snapshot for restore."""
    snap = {k: os.environ.pop(k, None) for k in ("SUTANDO_WORKSPACE",)}
    return snap


def _restore_env(snap):
    for k, v in snap.items():
        if v is not None:
            os.environ[k] = v


# -----------------------------------------------------------------------
# Cases
# -----------------------------------------------------------------------


def test_relative_env_path_is_anchored_to_cwd_absolute():
    """Bug fix regression guard. A relative `SUTANDO_WORKSPACE` MUST
    NOT survive resolution — anchor to CWD-at-resolve-time so two
    processes (different CWDs) don't silently split-brain on the same
    env value.

    Pin: result is absolute. The exact anchor depends on CWD, but it
    MUST end with the relative segment so a misconfigured user can
    still locate their workspace from the stderr warning."""
    snap = _clear_env()
    tmp = Path(tempfile.mkdtemp(prefix="sutando-ws-cwd-"))
    original_cwd = Path.cwd()
    os.chdir(tmp)
    os.environ["SUTANDO_WORKSPACE"] = "myws"  # deliberately relative
    try:
        result = ws.resolve_workspace(migrate=False)
        assert result.is_absolute(), (
            f"relative env survived as {result!r} — bug: a launchd-managed "
            "process and a bare-shell process would land in different dirs"
        )
        # Anchored to current CWD.
        assert result.name == "myws", f"expected ../myws, got {result}"
    finally:
        os.chdir(original_cwd)
        del os.environ["SUTANDO_WORKSPACE"]
        _restore_env(snap)


def test_absolute_env_path_unchanged():
    """Existing behavior preserved: an absolute `SUTANDO_WORKSPACE`
    is returned as-is. Backwards compatibility for every existing
    install."""
    snap = _clear_env()
    abs_path = Path(tempfile.mkdtemp(prefix="sutando-ws-abs-"))
    os.environ["SUTANDO_WORKSPACE"] = str(abs_path)
    try:
        result = ws.resolve_workspace(migrate=False)
        assert result == abs_path
    finally:
        del os.environ["SUTANDO_WORKSPACE"]
        _restore_env(snap)


def test_tilde_in_env_path_is_expanded():
    """`~` in `SUTANDO_WORKSPACE` is expanded to `$HOME`. Pin: a user
    who sets `SUTANDO_WORKSPACE=~/sutando` doesn't end up with a
    literal `~` directory. Existing contract."""
    snap = _clear_env()
    os.environ["SUTANDO_WORKSPACE"] = "~/sutando-tilde-test"
    try:
        result = ws.resolve_workspace(migrate=False)
        assert "~" not in str(result), f"tilde not expanded: {result}"
        assert str(result).startswith(str(Path.home()))
    finally:
        del os.environ["SUTANDO_WORKSPACE"]
        _restore_env(snap)


def test_migrate_from_legacy_moves_runtime_dirs():
    """`_migrate_from_legacy` should move tasks/, results/, state/,
    notes/ from a legacy repo with runtime evidence into a fresh
    target. Asserts the move happened, files preserved, and the legacy
    side is empty afterward."""
    tmp, legacy = _fresh_layout()
    # Populate legacy with runtime evidence.
    (legacy / "tasks").mkdir()
    (legacy / "tasks" / "task-1.txt").write_text("hello")
    (legacy / "state").mkdir()
    (legacy / "state" / "owner.json").write_text("{}")
    target = tmp / "workspace"
    original = _override_legacy(legacy)
    try:
        moved = ws._migrate_from_legacy(target)
        assert moved is True
        assert (target / "tasks" / "task-1.txt").read_text() == "hello"
        assert (target / "state" / "owner.json").read_text() == "{}"
        assert not (legacy / "tasks").exists(), "legacy side not cleaned"
        assert not (legacy / "state").exists()
    finally:
        _restore_legacy(original)


def test_migrate_from_legacy_is_idempotent():
    """Second call with the target already populated must return False
    and NOT re-touch anything. Pre-fix the migration could collide
    with itself on retry."""
    tmp, legacy = _fresh_layout()
    target = tmp / "workspace"
    target.mkdir()
    (target / "tasks").mkdir()
    (target / "tasks" / "task-existing.txt").write_text("existing")
    # Also put something in legacy so we'd otherwise trigger.
    (legacy / "tasks").mkdir()
    (legacy / "tasks" / "task-from-legacy.txt").write_text("legacy")
    original = _override_legacy(legacy)
    try:
        moved = ws._migrate_from_legacy(target)
        # Target had content → migration bails (existing user has a
        # working setup; we don't merge).
        assert moved is False
        # And the legacy file should still be on the legacy side.
        assert (legacy / "tasks" / "task-from-legacy.txt").exists()
        # Existing target content untouched.
        assert (target / "tasks" / "task-existing.txt").read_text() == "existing"
    finally:
        _restore_legacy(original)


def test_migrate_from_legacy_does_not_fire_without_runtime_evidence():
    """If legacy has the dirs but they're empty (no actual workspace
    use), don't migrate. Otherwise every developer who runs `git
    clone` would have empty tasks/results/state migrated, creating
    confusing empty dirs in their workspace."""
    tmp, legacy = _fresh_layout()
    # Empty dirs — no runtime evidence.
    (legacy / "tasks").mkdir()
    (legacy / "results").mkdir()
    (legacy / "state").mkdir()
    target = tmp / "workspace"
    original = _override_legacy(legacy)
    try:
        moved = ws._migrate_from_legacy(target)
        assert moved is False, "migrated empty dirs — should require evidence"
        # Target should not have been created.
        assert not target.exists(), f"empty target created unnecessarily: {target}"
    finally:
        _restore_legacy(original)


def test_migrate_inrepo_notes_is_flat_only():
    """The flat-notes contract: top-level `.md`/`.txt` files in
    `<repo>/notes/` migrate; subdirectories (e.g. `notes/projects/`)
    are intentionally LEFT in place. Pin so a future "recursive=True
    by default" change has to update this test deliberately."""
    tmp, legacy = _fresh_layout()
    (legacy / "notes").mkdir()
    (legacy / "notes" / "flat-note.md").write_text("flat")
    (legacy / "notes" / "skip.txt").write_text("also flat")
    (legacy / "notes" / "projects").mkdir()
    (legacy / "notes" / "projects" / "nested.md").write_text("nested")
    target = tmp / "workspace"
    original = _override_legacy(legacy)
    try:
        moved = ws._migrate_inrepo_notes(target)
        assert moved is True
        # Flat files migrated.
        assert (target / "notes" / "flat-note.md").read_text() == "flat"
        assert (target / "notes" / "skip.txt").read_text() == "also flat"
        # Subdir LEFT in place at legacy — not migrated.
        assert (legacy / "notes" / "projects" / "nested.md").exists(), (
            "subdir contents migrated — breaks the flat-notes contract"
        )
        assert not (target / "notes" / "projects").exists(), (
            "subdir migrated to workspace — breaks the flat-notes contract"
        )
        # Sentinel dropped for O(1) re-entry.
        assert (target / ".notes-migrated").exists()
    finally:
        _restore_legacy(original)


def test_migrate_inrepo_build_log_is_non_destructive_on_collision():
    """If the workspace already has `build_log.md`, the in-repo copy
    must NOT clobber it. Workspace is authoritative once it exists —
    losing the user's current build log to a stale repo copy would be
    a serious regression."""
    tmp, legacy = _fresh_layout()
    target = tmp / "workspace"
    target.mkdir()
    # Workspace already has the authoritative build_log.
    (target / "build_log.md").write_text("workspace-authoritative")
    # Repo has a stale copy.
    (legacy / "build_log.md").write_text("STALE-do-not-migrate")
    original = _override_legacy(legacy)
    try:
        moved = ws._migrate_inrepo_build_log(target)
        assert moved is False, "migration overwrote workspace's build_log"
        # The workspace copy survives unchanged.
        assert (target / "build_log.md").read_text() == "workspace-authoritative"
        # Repo copy left alone (intentional — let admin decide to delete).
        assert (legacy / "build_log.md").read_text() == "STALE-do-not-migrate"
    finally:
        _restore_legacy(original)


def main():
    test_relative_env_path_is_anchored_to_cwd_absolute()
    test_absolute_env_path_unchanged()
    test_tilde_in_env_path_is_expanded()
    test_migrate_from_legacy_moves_runtime_dirs()
    test_migrate_from_legacy_is_idempotent()
    test_migrate_from_legacy_does_not_fire_without_runtime_evidence()
    test_migrate_inrepo_notes_is_flat_only()
    test_migrate_inrepo_build_log_is_non_destructive_on_collision()
    print("All workspace_default migration tests passed.")


if __name__ == "__main__":
    main()
