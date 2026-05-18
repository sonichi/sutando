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
_LEGACY_DIRS = ("tasks", "results", "state", "notes")  # the runtime-state dirs that, if
                                                       # found in a legacy fallback location,
                                                       # signal an in-use older install we
                                                       # should migrate. `notes` joined the
                                                       # list 2026-05-16; in-repo→workspace
                                                       # migration for env-set installs is
                                                       # handled by `_migrate_inrepo_notes`
                                                       # below (different trigger condition
                                                       # — see its docstring).


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
    # Originally checked only for "task-*" files, which missed observer-only
    # nodes (no tasks written, but results/ or state/ are populated — e.g. a
    # secondary node that only reads results). Widened per issue #770.
    runtime_evidence = False
    for d in _LEGACY_DIRS:
        legacy_d = legacy / d
        if not legacy_d.is_dir():
            continue
        # task-* → primary node evidence. Any file in results/ or state/ →
        # observer-node evidence. notes/ with files → user note-taking node.
        if any(legacy_d.iterdir()):
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


def _migrate_inrepo_notes(workspace: Path) -> bool:
    """One-time migration of `<repo>/notes/*` -> `<workspace>/notes/` when env-set
    workspaces have stragglers in the in-repo location.

    Different trigger than `_migrate_from_legacy`: that one fires only when
    `$SUTANDO_WORKSPACE` is UNSET (legacy install upgrading to the new default).
    THIS one fires when the env IS set, BUT the in-repo `notes/` dir (which
    code used to write to before the workspace contract) still contains files
    that aren't under `<workspace>/notes/`. The trigger condition is
    "workspace and in-repo location are different, AND in-repo has notes."

    Scope: top-level `.md`/`.txt` files only. Subdirectories (e.g.
    `notes/projects/`, `notes/media/`) are intentionally NOT migrated —
    workspace notes are flat by current convention. If/when nested notes
    become supported, this migrator grows a `recursive=True` flag rather
    than silently changing posture. Owner's notes layout (2026-05-16) is
    flat; this matches.

    Symmetric to `_migrate_from_legacy`'s posture: non-destructive on collision
    (skip the file if it already exists at the workspace location), logs each
    moved file to stderr, idempotent (second run finds in-repo empty and
    bails). Also writes a sentinel (`<workspace>/.notes-migrated`) after a
    successful run so subsequent `resolve_workspace()` calls short-circuit
    on the cheap stat-check rather than re-running iterdir; per Lucy's #769
    review obs 2.

    Per owner directive 2026-05-16: every design change must ship with an
    automatic migration script so existing users don't have to migrate
    manually. This is the migration for the notes-location change.

    Returns True iff at least one file was migrated.
    """
    # Sentinel-file short-circuit: after a previous successful migration we
    # leave a marker so this function exits in O(1) instead of O(directory
    # listing) on every bridge restart. Per Lucy's #769 review obs 2.
    sentinel = workspace / ".notes-migrated"
    if sentinel.exists():
        return False
    repo_root = _legacy_repo_root()
    # If workspace IS the repo root, there's nothing to migrate (both names
    # resolve to the same dir). Owner's case: workspace = <repo>/workspace/,
    # different from repo root, migration applies.
    try:
        if repo_root.resolve() == workspace.resolve():
            return False
    except OSError:
        return False
    inrepo_notes = repo_root / "notes"
    if not inrepo_notes.is_dir():
        return False
    # Only operate on regular md/txt files at the top level of in-repo notes/.
    # Subdirectories (e.g. `notes/media/`, `notes/projects/`) and the historic
    # memory-sync symlink convention are left alone — they may have their own
    # semantics this migration shouldn't touch. See function docstring on
    # the flat-notes convention.
    candidates = [p for p in inrepo_notes.iterdir()
                  if p.is_file() and p.suffix in (".md", ".txt")]
    if not candidates:
        # No top-level notes to migrate — drop the sentinel anyway so we
        # don't iterdir again on next call (Lucy obs 2). Cheap touch.
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
        except Exception:
            pass  # sentinel is an optimization, never fatal
        return False
    target_notes = workspace / "notes"
    target_notes.mkdir(parents=True, exist_ok=True)
    moved = []
    for src in candidates:
        dst = target_notes / src.name
        if dst.exists():
            continue  # don't clobber an existing file at the workspace location
        try:
            shutil.move(str(src), str(dst))
            moved.append(src.name)
        except Exception as e:  # pragma: no cover — best-effort
            print(f"notes migration: failed to move {src} -> {dst}: {e}", file=sys.stderr)
    if moved:
        print(
            f"notes migration: moved {len(moved)} file(s) from {inrepo_notes} "
            f"to {target_notes} (one-time: {', '.join(moved[:5])}"
            f"{', …' if len(moved) > 5 else ''})",
            file=sys.stderr,
        )
    # Drop the sentinel so subsequent calls short-circuit on the cheap exists()
    # check (Lucy's #769 obs 2). Best-effort; if the touch fails we'll just
    # iterdir() again next call — correctness unaffected.
    try:
        sentinel.touch()
    except Exception:
        pass
    return bool(moved)


def _migrate_inrepo_build_log(workspace: Path) -> bool:
    """One-time migration of `<repo>/build_log.md` -> `<workspace>/build_log.md`.

    Parallel to `_migrate_inrepo_notes`: fires regardless of env state when the
    in-repo `build_log.md` exists and workspace != repo root. Build_log is a
    single file (unlike notes which is a dir), so the logic is simpler.

    Per workspace contract (CLAUDE.md): build_log.md is a per-user mutable
    runtime artifact and belongs in the workspace, not the repo. Historic
    placement at the repo root polluted `git status` and split-brained
    dashboard.py / health-check.py (which already read from workspace) vs
    voice-context.ts / sync-memory.sh (which still wrote to repo). This
    migration fixes the split.

    Non-destructive on collision (skip if workspace already has build_log.md),
    sentinel-gated for O(1) re-entry (`<workspace>/.build_log-migrated`).
    """
    sentinel = workspace / ".build_log-migrated"
    if sentinel.exists():
        return False
    repo_root = _legacy_repo_root()
    try:
        if repo_root.resolve() == workspace.resolve():
            return False
    except OSError:
        return False
    inrepo_build_log = repo_root / "build_log.md"
    if not inrepo_build_log.is_file():
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
        except Exception:
            pass
        return False
    target = workspace / "build_log.md"
    if target.exists():
        try:
            sentinel.touch()
        except Exception:
            pass
        return False
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(inrepo_build_log), str(target))
        print(
            f"build_log migration: moved {inrepo_build_log} -> {target} (one-time)",
            file=sys.stderr,
        )
    except Exception as e:  # pragma: no cover — best-effort
        print(f"build_log migration: failed: {e}", file=sys.stderr)
        return False
    try:
        sentinel.touch()
    except Exception:
        pass
    return True


def resolve_workspace(migrate: bool = True) -> Path:
    """Resolve the workspace directory per the canonical contract.

    Order:
      1. `$SUTANDO_WORKSPACE` env var, expanded (`~` honored).
      2. `~/.sutando/workspace/`.

    When `migrate=True` (default) the function ALSO runs one-time
    auto-migrations:
      • `_migrate_from_legacy` — fires when env is unset and the new default
        doesn't yet exist; pulls tasks/results/state/notes from the legacy
        repo-root fallback.
      • `_migrate_inrepo_notes` — fires when workspace != repo root; pulls
        top-level notes/*.md|*.txt from the in-repo `notes/` dir into
        `<workspace>/notes/`.
      • `_migrate_inrepo_build_log` — fires when workspace != repo root; moves
        in-repo `build_log.md` to `<workspace>/build_log.md`.

    Returns a `Path` — does NOT create the directory; the caller decides.
    Pass `migrate=False` from tests that want pure resolution semantics.
    """
    env = os.environ.get("SUTANDO_WORKSPACE", "").strip()
    if env:
        target = Path(env).expanduser()
        if migrate:
            try:
                _migrate_inrepo_notes(target)
            except Exception as e:  # pragma: no cover — must never break resolution
                print(f"notes migration: skipped due to error: {e}", file=sys.stderr)
            try:
                _migrate_inrepo_build_log(target)
            except Exception as e:  # pragma: no cover — must never break resolution
                print(f"build_log migration: skipped due to error: {e}", file=sys.stderr)
        return target
    target = default_workspace_dir()
    if migrate:
        try:
            _migrate_from_legacy(target)
        except Exception as e:  # pragma: no cover — must never break resolution
            print(f"workspace migration: skipped due to error: {e}", file=sys.stderr)
        try:
            _migrate_inrepo_build_log(target)
        except Exception as e:  # pragma: no cover — must never break resolution
            print(f"build_log migration: skipped due to error: {e}", file=sys.stderr)
    return target
