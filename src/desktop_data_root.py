"""The AG2 Space desktop app's macOS data folder, for code that runs without the launcher's env.

Resolution order (mirrored by `scripts/sutando-config.sh desktop-data-root`):

  1. $SUTANDO_APP_SUPPORT, when set and non-empty (exported by the launcher)
  2. the desktop's own rule, `ag2_app_support_dir` in the `app-support.sh` it
     ships beside this checkout (`<data root>/engine/app-support.sh`)
  3. the built-in copy of that rule, when the shipped file is absent or fails:
     ~/Library/Application Support/space.ag2.desktop when the
     `.space.ag2.desktop.ready` marker exists, or
     `space.ag2.desktop/.copied-from-space.ag2.app` is a file, or there is no
     space.ag2.app directory; else ~/Library/Application Support/space.ag2.app

Derived paths (run dir, bundled node) hang off this root; their own more
specific env overrides keep priority in their callers.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping, Optional

DESKTOP_DIR_NAME = "space.ag2.desktop"
LEGACY_DIR_NAME = "space.ag2.app"
READY_MARKER = ".space.ag2.desktop.ready"
COPIED_RECORD = ".copied-from-space.ag2.app"

# Logical path, like the shell's `cd .. && pwd`: <checkout>/../app-support.sh.
SHIPPED_RULE = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).parent / "app-support.sh"
SHIPPED_TIMEOUT_S = 2.0
_CALL_SHIPPED = ('. "$1" >/dev/null 2>&1 && declare -F ag2_app_support_dir >/dev/null'
                 ' && ag2_app_support_dir "$2"')


def builtin_data_root(home: Path) -> Path:
    base = Path(home) / "Library" / "Application Support"
    current = base / DESKTOP_DIR_NAME
    if ((base / READY_MARKER).exists() or (current / COPIED_RECORD).is_file()
            or not (base / LEGACY_DIR_NAME).is_dir()):
        return current
    return base / LEGACY_DIR_NAME


def shipped_data_root(home: Path, shipped: Path) -> Optional[Path]:
    """The desktop's own rule, or None when it is absent, fails or prints nothing."""
    if not shipped.is_file():
        return None
    try:
        proc = subprocess.run(["bash", "-c", _CALL_SHIPPED, "_", str(shipped), str(home)],
                              capture_output=True, text=True, timeout=SHIPPED_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.rstrip("\n")
    if proc.returncode != 0 or not out or "\n" in out:
        return None
    return Path(out)


def desktop_data_root(env: Optional[Mapping[str, str]] = None,
                      home: Optional[Path] = None,
                      shipped: Optional[Path] = None) -> Path:
    env = os.environ if env is None else env
    explicit = env.get("SUTANDO_APP_SUPPORT") or ""
    if explicit:
        return Path(explicit)
    home = Path.home() if home is None else Path(home)
    from_desktop = shipped_data_root(home, SHIPPED_RULE if shipped is None else Path(shipped))
    return from_desktop if from_desktop is not None else builtin_data_root(home)
