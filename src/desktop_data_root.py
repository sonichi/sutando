"""The AG2 Space desktop app's macOS data folder, for code that runs without the launcher's env.

Resolution order (mirrored by `scripts/sutando-config.sh desktop-data-root`):

  1. $SUTANDO_APP_SUPPORT, when set and non-empty (exported by the launcher)
  2. ~/Library/Application Support/space.ag2.desktop, when the move finished
     (the `.space.ag2.desktop.ready` marker exists and the folder is a directory)
     or when there is no space.ag2.app directory at all (a fresh install)
  3. ~/Library/Application Support/space.ag2.app otherwise: not migrated yet,
     or a move that never completed

Derived paths (run dir, bundled node) hang off this root; their own more
specific env overrides keep priority in their callers.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

DESKTOP_DIR_NAME = "space.ag2.desktop"
LEGACY_DIR_NAME = "space.ag2.app"
READY_MARKER = ".space.ag2.desktop.ready"


def desktop_data_root(env: Optional[Mapping[str, str]] = None,
                      home: Optional[Path] = None) -> Path:
    env = os.environ if env is None else env
    explicit = env.get("SUTANDO_APP_SUPPORT") or ""
    if explicit:
        return Path(explicit)
    base = (Path.home() if home is None else Path(home)) / "Library" / "Application Support"
    current = base / DESKTOP_DIR_NAME
    legacy = base / LEGACY_DIR_NAME
    moved = (base / READY_MARKER).exists() and current.is_dir()
    if moved or not legacy.is_dir():
        return current
    return legacy

