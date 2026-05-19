"""Resolve personal-asset paths with private-dir-first lookup.

Each Stand has its own identity + avatar. These files are gitignored and
machine-local. Canonical home is `$SUTANDO_MEMORY_DIR/machine-<hostname>/`
so they live with the rest of the per-machine memory under the private
sync repo. Public-workspace fallback is preserved so existing installs
keep working until they migrate.

The env var `SUTANDO_MEMORY_DIR` is the canonical name per the 2026-05-18
workspace-design RFC (#858, Decision 2). The legacy name `SUTANDO_PRIVATE_DIR`
is honored as a fallback for one release with a deprecation warning on
every read (cron environments miss startup-only warnings, so logging at
every resolution is intentional).

Usage:
    from util_paths import personal_path
    si = personal_path("stand-identity.json")
    avatar = personal_path("stand-avatar.png")  # also tries assets/ in public
"""
from __future__ import annotations
import os
import socket
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent


def _memory_dir_env() -> str | None:
    """Return the resolved memory-dir env value, preferring the new name.

    Lookup order:
      1. `SUTANDO_MEMORY_DIR` (canonical post-#858 / #870)
      2. `SUTANDO_PRIVATE_DIR` (legacy, with deprecation warning emitted
         to stderr on every read — not just once at startup; cron and
         launchd environments miss startup-only warnings).

    Returns the raw env value (caller must `os.path.expanduser` if needed),
    or None when neither is set."""
    new = os.environ.get("SUTANDO_MEMORY_DIR")
    if new:
        return new
    legacy = os.environ.get("SUTANDO_PRIVATE_DIR")
    if legacy:
        # Every-read deprecation warning. This is loud by design — the
        # legacy alias will drop in the next release and silent users
        # would otherwise miss the cutover. See #870 for the rename plan.
        print(
            "[util_paths.py] DEPRECATION: SUTANDO_PRIVATE_DIR is the old name "
            "for the memory dir; set SUTANDO_MEMORY_DIR instead (this alias "
            "will be removed in the next release). See #870.",
            file=sys.stderr,
        )
        return legacy
    return None


def _workspace_root() -> Path:
    """Workspace root for runtime-state paths.

    Per the workspace contract (docs/workspace-contract.md): REPO_DIR is
    SOURCE-TREE-ONLY (exec'ing source files, git cwd, reading checked-in
    files). All user/runtime paths go through the workspace. Delegates to
    workspace_default.resolve_workspace() so SUTANDO_WORKSPACE, the
    canonical default (~/.sutando/workspace/), and PR #762's one-time
    legacy migration are all honored in one call.

    `migrate=False` — path resolution shouldn't trigger migrations on
    every call. Migration runs from src/startup.sh and the bridge boot
    paths where it belongs.
    """
    try:
        from workspace_default import resolve_workspace
        return resolve_workspace(migrate=False)
    except ImportError:
        # Inline fallback. NEVER REPO_DIR — that's source-tree, not workspace.
        env = os.environ.get("SUTANDO_WORKSPACE")
        if env:
            return Path(os.path.expanduser(env))
        return Path.home() / ".sutando" / "workspace"


def _private_machine_dir() -> Path | None:
    root = _memory_dir_env()
    if not root:
        return None
    expanded = os.path.expanduser(root)
    host = socket.gethostname().split(".")[0]
    return Path(expanded) / f"machine-{host}"


def personal_path(filename: str, workspace: Path | None = None) -> Path:
    """Resolve a personal-asset path.

    Order: `$SUTANDO_MEMORY_DIR/machine-<host>/<filename>` → `<workspace>/<filename>`.
    (Legacy `$SUTANDO_PRIVATE_DIR` is honored as a fallback with a
    deprecation warning — see `_memory_dir_env()`.)
    For files known to live under `assets/` in the public workspace
    (currently `stand-avatar.png`), also tries `<workspace>/assets/<filename>`
    before falling back to `<workspace>/<filename>`.

    Returns the FIRST existing path. If none exist, returns the preferred
    private-dir path so the caller's `.exists()` check fails gracefully.
    """
    ws = workspace if workspace is not None else _workspace_root()

    private = _private_machine_dir()
    if private is not None:
        p = private / filename
        if p.exists():
            return p

    # Public workspace — assets/ first for avatar-style files, then root
    if filename in {"stand-avatar.png"}:
        p = ws / "assets" / filename
        if p.exists():
            return p

    p = ws / filename
    if p.exists():
        return p

    # Nothing exists; return preferred (private if configured, else workspace)
    if private is not None:
        return private / filename
    if filename in {"stand-avatar.png"}:
        return ws / "assets" / filename
    return ws / filename


def shared_personal_path(filename: str, workspace: Path | None = None) -> Path:
    """Resolve a shared-private path (notes, build_log, etc.) — files that
    sync across all of an owner's machines, not per-machine state.

    Order: `$SUTANDO_MEMORY_DIR/<filename>` (top-level, shared) → `<workspace>/<filename>`.
    (Legacy `$SUTANDO_PRIVATE_DIR` is honored as a fallback with a
    deprecation warning — see `_memory_dir_env()`.)

    Difference vs `personal_path`: this resolves to the top-level private dir,
    NOT `machine-<host>/`. Use for files like notes/, where every Mac in
    Chi's fleet should see the same content.

    Returns the FIRST existing path. If none exist, returns the preferred
    private path so the caller's `.exists()` check fails gracefully.
    """
    ws = workspace if workspace is not None else _workspace_root()

    root = _memory_dir_env()
    if root:
        private = Path(os.path.expanduser(root)) / filename
        if private.exists():
            return private
        # Fall back to workspace if private doesn't have it, but remember
        # the preferred private path for the "nothing exists" branch.
        p = ws / filename
        if p.exists():
            return p
        return private

    p = ws / filename
    return p


# ---------------------------------------------------------------------------
# Claude Code home directory — the host CLI's per-user state lives at
# `~/.claude/`. Sutando consumes several subpaths (channels/, projects/,
# skills/, settings.json, etc.); centralizing the resolution here keeps the
# host-CLI dependency surface a single grep.
#
# Why this helper: per the 2026-05-18 workspace-design RFC discussion, the
# dependency on `~/.claude/` is real (memory storage, channel tokens, skill
# discovery, slash-command write convention) and we accept it operationally —
# but we want the surface countable so a future swap is a 1-day grep+replace
# rather than a re-architecture. ANY new read/write into the Claude Code home
# directory should go through this helper.
#
# Resolution: prefer $CLAUDE_HOME if set (override / testing), else
# `~/.claude/`. Does NOT create the dir.
# ---------------------------------------------------------------------------

def claude_home_path(*subpath: str) -> Path:
    """Resolve a path under Claude Code's per-user home (`~/.claude/`).

    Pass subpath components positionally, e.g.:
        claude_home_path("channels", "discord", "access.json")
        claude_home_path("projects", project_slug, "memory", "MEMORY.md")
        claude_home_path("skills", skill_name)

    Override the base with `$CLAUDE_HOME` for tests + alt-host installs.
    """
    base_env = os.environ.get("CLAUDE_HOME")
    base = Path(os.path.expanduser(base_env)) if base_env else (Path.home() / ".claude")
    if not subpath:
        return base
    return base.joinpath(*subpath)
