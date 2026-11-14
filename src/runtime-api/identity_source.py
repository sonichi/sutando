#!/usr/bin/env python3
"""Where the identity surface reads from, and the one IdentityView built
from it. The daemon and the CLI's no-daemon read both construct through
build_identity_view, so they cannot drift onto different channel or host
sources.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from identity_view import IdentityView
from rundir import instance_id

_HERE = Path(__file__).resolve().parent


def channels_dir() -> str | None:
    """Channel access configs via the canonical resolver (util_paths); None
    if unresolvable — the identity surface then simply omits channel data."""
    try:
        sys.path.insert(0, str(_HERE.parent))
        from util_paths import claude_home_path  # noqa: PLC0415
        return str(claude_home_path("channels"))
    except Exception:  # noqa: BLE001
        return None


def host_label() -> str | None:
    """This host's per-host label (matches the cores heartbeat basename).
    SUTANDO_HOST_LABEL wins, else the canonical resolver script; best-effort."""
    env = os.environ.get("SUTANDO_HOST_LABEL")
    if env:
        return env
    try:
        out = subprocess.run(
            ["bash", str(_HERE.parent.parent / "scripts" / "sutando-config.sh"),
             "host-label"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def build_identity_view(state_dir, actor_id: str,
                        label: str | None) -> IdentityView:
    return IdentityView(state_dir, actor_id, channels_dir=channels_dir(),
                        host_label=label, instance=instance_id())
