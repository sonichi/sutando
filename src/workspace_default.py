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
from pathlib import Path


_DEFAULT_SUBPATH = (".sutando", "workspace")


def default_workspace_dir() -> Path:
    """Return `~/.sutando/workspace/`."""
    return Path.home().joinpath(*_DEFAULT_SUBPATH)


def resolve_workspace() -> Path:
    """Resolve the workspace directory per the canonical contract.

    Order:
      1. `$SUTANDO_WORKSPACE` env var, expanded (`~` honored).
      2. `~/.sutando/workspace/`.

    Returns a `Path` — does NOT create the directory; the caller decides.
    """
    env = os.environ.get("SUTANDO_WORKSPACE", "").strip()
    if env:
        return Path(env).expanduser()
    return default_workspace_dir()
