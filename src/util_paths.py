"""Resolve personal-asset paths with private-dir-first lookup.

Each Stand has its own identity + avatar. These files are gitignored and
machine-local. Canonical home is `$SUTANDO_PRIVATE_DIR/machine-<hostname>/`
so they live with the rest of the per-machine memory under the private
sync repo. Public-workspace fallback is preserved so existing installs
keep working until they migrate.

Usage:
    from util_paths import personal_path
    si = personal_path("stand-identity.json")
    avatar = personal_path("stand-avatar.png")  # also tries assets/ in public
"""
from __future__ import annotations
import os
import socket
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent


def _private_machine_dir() -> Path | None:
    root = os.environ.get("SUTANDO_PRIVATE_DIR")
    if not root:
        return None
    expanded = os.path.expanduser(root)
    host = socket.gethostname().split(".")[0]
    return Path(expanded) / f"machine-{host}"


def personal_path(filename: str, workspace: Path | None = None) -> Path:
    """Resolve a personal-asset path.

    Order: `$SUTANDO_PRIVATE_DIR/machine-<host>/<filename>` → `<workspace>/<filename>`.
    For files known to live under `assets/` in the public workspace
    (currently `stand-avatar.png`), also tries `<workspace>/assets/<filename>`
    before falling back to `<workspace>/<filename>`.

    Returns the FIRST existing path. If none exist, returns the preferred
    private-dir path so the caller's `.exists()` check fails gracefully.
    """
    ws = workspace if workspace is not None else REPO_DIR

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

    Order: `$SUTANDO_PRIVATE_DIR/<filename>` (top-level, shared) → `<workspace>/<filename>`.

    Difference vs `personal_path`: this resolves to the top-level private dir,
    NOT `machine-<host>/`. Use for files like notes/, where every Mac in
    Chi's fleet should see the same content.

    Returns the FIRST existing path. If none exist, returns the preferred
    private path so the caller's `.exists()` check fails gracefully.
    """
    ws = workspace if workspace is not None else REPO_DIR

    root = os.environ.get("SUTANDO_PRIVATE_DIR")
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
