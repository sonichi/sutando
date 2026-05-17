"""Canonical workspace-directory resolution for Sutando services.

All runtime artifacts (tasks/, results/, state/, data/, build_log.md, ...) live
under the workspace dir. Components MUST consult `SUTANDO_WORKSPACE` first;
when unset, fall back to `~/.sutando/workspace/` — a hidden, OS-neutral home-
relative path that stays out of Sutando.app's `~/Library/Application Support/
sutando/` (which owns Chromium-style cache: Cache/, GPUCache/, Cookies/,
blob_storage/, etc.).

Historic anti-pattern: bridges fell back to `Path(__file__).resolve().parent.parent`
which resolved to the repo root, polluting `git status` with runtime artifacts
on bare-shell launches that forgot to set the env. Worse, when invoked from an
app-bundled `src/` symlink, it walked into the bundle and stranded owner DMs
(tasks landed in bundle-tasks/ while the watcher polled workspace-tasks/).
"""
from __future__ import annotations
import os
import shutil
import sys
from pathlib import Path


_DEFAULT_SUBPATH = (".sutando", "workspace")
_LEGACY_DIRS = ("tasks", "results", "state")  # the runtime-state dirs that, if found
                                              # in a legacy fallback location, signal an
                                              # in-use older install we should migrate.


def default_workspace_dir() -> Path:
    """Return `~/.sutando/workspace/`."""
    return Path.home().joinpath(*_DEFAULT_SUBPATH)


def _legacy_repo_root() -> Path:
    """Where the historic `Path(__file__).resolve().parent.parent` fallback
    pointed for this checkout — i.e. the sutando repo root that contains this
    helper. Used only for one-time auto-migration; never as a resolver fallback."""
    return Path(__file__).resolve().parent.parent


def _migrate_from_legacy(target: Path) -> bool:
    """Move runtime-state dirs from the legacy repo-root fallback into `target`
    on first run after the workspace-default change.

    Triggers only when ALL of:
      • `$SUTANDO_WORKSPACE` is unset (user hasn't pinned a path).
      • `target` (the new default) does NOT yet exist.
      • The legacy repo root contains at least one of {tasks/, results/, state/}
        with task-* files inside (i.e. it WAS actively used as a workspace).

    Action: create `target`, move the dirs in, log a single stderr line per dir.
    Idempotent: second run sees `target` exists and bails. Non-destructive on
    the legacy side: only moves dirs we recognize as runtime-state, never
    deletes or touches anything else.

    Returns True iff at least one dir was migrated.
    """
    legacy = _legacy_repo_root()
    if not legacy.exists():
        return False
    # Don't migrate if target already has any content — assume user already
    # has a working setup at the new path.
    if target.exists() and any(target.iterdir()):
        return False
    # Look for runtime evidence in the legacy root before doing anything.
    runtime_evidence = False
    for d in _LEGACY_DIRS:
        legacy_d = legacy / d
        if legacy_d.is_dir() and any(legacy_d.glob("task-*")):
            runtime_evidence = True
            break
    if not runtime_evidence:
        return False
    target.mkdir(parents=True, exist_ok=True)
    moved = []
    for d in _LEGACY_DIRS:
        src = legacy / d
        if src.is_dir():
            dst = target / d
            if dst.exists():
                continue  # don't clobber
            try:
                shutil.move(str(src), str(dst))
                moved.append(d)
            except Exception as e:  # pragma: no cover — best-effort
                print(f"workspace migration: failed to move {src} -> {dst}: {e}", file=sys.stderr)
    if moved:
        print(
            f"workspace migration: moved {', '.join(moved)} from {legacy} to {target} (one-time)",
            file=sys.stderr,
        )
    return bool(moved)


def resolve_workspace(migrate: bool = True) -> Path:
    """Resolve the workspace directory per the canonical contract.

    Order:
      1. `$SUTANDO_WORKSPACE` env var, expanded (`~` honored).
      2. `~/.sutando/workspace/`.

    When `migrate=True` (default) AND the env is unset AND the new default
    doesn't yet exist, also performs one-time auto-migration of runtime-state
    dirs from the legacy repo-root fallback. See `_migrate_from_legacy` for
    the exact conditions.

    Returns a `Path` — does NOT create the directory; the caller decides.
    Pass `migrate=False` from tests that want pure resolution semantics.
    """
    env = os.environ.get("SUTANDO_WORKSPACE", "").strip()
    if env:
        return Path(env).expanduser()
    target = default_workspace_dir()
    if migrate:
        try:
            _migrate_from_legacy(target)
        except Exception as e:  # pragma: no cover — must never break resolution
            print(f"workspace migration: skipped due to error: {e}", file=sys.stderr)
    return target
