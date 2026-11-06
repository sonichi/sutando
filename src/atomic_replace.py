"""Atomic publication with bounded retries for Windows sharing violations."""
from __future__ import annotations

import os
import time
from pathlib import Path

_WINDOWS_REPLACE_ATTEMPTS = 50
_WINDOWS_REPLACE_DELAY_S = 0.01


def replace_snapshot(tmp: Path, target: Path) -> None:
    """Publish atomically, retrying transient Windows sharing violations."""
    for attempt in range(_WINDOWS_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt + 1 == _WINDOWS_REPLACE_ATTEMPTS:
                raise
            time.sleep(_WINDOWS_REPLACE_DELAY_S)

