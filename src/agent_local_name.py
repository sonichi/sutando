"""This agent's display name for text the owner reads, from local records only.

`agent_display_name.resolve()` is the name for a self-introduction: it asks the homeserver first
and reads the channel credentials to find who to ask about. A monitor that titles a card or a
notice may do neither, so this reads only what is already on disk and in the environment:

1. the `agent_display_name` the gateway listed on the primary connection row
   (<workspace>/state/connections.json);
2. SUTANDO_DISPLAY_NAME, else AGENT_DISPLAY_NAME, from the environment.

A name that is an identifier (a session name, an id) is no name; then, and when nothing is
recorded, the answer is `default`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

from owner_text import DEFAULT_AGENT_NAME, named

CONFIGURED_VARS = ("SUTANDO_DISPLAY_NAME", "AGENT_DISPLAY_NAME")


def _connection_name(workspace: Optional[Path]) -> str:
    import connections
    row = connections.primary_row(workspace)
    return str((row or {}).get("agent_display_name") or "")


def local_name(default: str = DEFAULT_AGENT_NAME, *, workspace: Optional[Path] = None,
               environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    try:
        recorded = _connection_name(workspace)
    except Exception:  # noqa: BLE001 - a name is never worth failing the notice it titles
        recorded = ""
    for candidate in [recorded] + [env.get(var) or "" for var in CONFIGURED_VARS]:
        name = named(candidate, "")
        if name:
            return name
    return default
