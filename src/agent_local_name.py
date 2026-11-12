"""This agent's display name for text the owner reads, without a network call.

`agent_display_name.resolve(offline=True)`: the name the agent introduces itself by, minus the
homeserver lookup (a monitor that titles a card or a notice may not make one): the connection row
for this agent's mxid, the configured name, then its mxid's localpart. A name that is an
identifier (a session name, an id) is no name; then, and when nothing is recorded, `default`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

import agent_display_name
from owner_text import DEFAULT_AGENT_NAME, named


def local_name(default: str = DEFAULT_AGENT_NAME, *, workspace: Optional[Path] = None,
               environ: Optional[Mapping[str, str]] = None, mxid: Optional[str] = None) -> str:
    try:
        name, source = agent_display_name.resolve(mxid, environ=environ, workspace=workspace, offline=True)
    except Exception:  # noqa: BLE001 - a name is never worth failing the notice it titles
        return default
    return default if source == "fallback" else named(name, default)
