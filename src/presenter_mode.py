"""Provider-neutral presenter-mode sentinel policy.

Presenter mode suppresses notifications while a workspace sentinel contains a
future ISO-8601 UTC expiry. Channel adapters own how they suppress delivery;
this module owns the shared state path and expiry interpretation.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from workspace_default import resolve_workspace


def presenter_mode_active(workspace: Optional[Path] = None, *, now: Optional[float] = None) -> bool:
    """Return whether the workspace's presenter-mode sentinel is unexpired.

    Missing, malformed, and unreadable sentinels fail closed so a damaged state
    file cannot suppress notifications indefinitely.
    """
    resolved_workspace = workspace if workspace is not None else resolve_workspace()
    sentinel = resolved_workspace / "state" / "presenter-mode.sentinel"
    if not sentinel.exists():
        return False
    try:
        expire_iso = sentinel.read_text().strip()
        # Full UTC shape, not just a leading digit: '9999-not-a-date' starts
        # with a digit and lexically compares as future, so anything less than
        # the whole pattern lets a corrupted sentinel suppress notifications
        # forever (#2516 review — shared canary with the TS twin).
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", expire_iso):
            return False
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        return now_iso < expire_iso
    except Exception:
        return False
