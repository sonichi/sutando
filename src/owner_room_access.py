"""How a room-database adapter reaches the owner's room: capability lookup, owner DM, agent identity and credentials.

The capability's scripts directory is found among the roots and skill names the adapter
injects; the owner's DM room and the agent identity come from the gateway's own reading. Names
no skill and imports none: each adapter passes its roots, the capability's skill names and
modules, and the loaded capability module. Standard library only, plus the shared channel
env resolvers loaded when a credential fallback is needed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

from workspace_default import status_path

IDENTITY_VARS = ("AG2SPACE_USER_ID", "AG2_MATRIX_USER_ID")


def capability_scripts(roots: Iterable[Path], names: Iterable[str], modules: Iterable[str]) -> Optional[Path]:
    """The first `<root>/<name>/scripts` holding every one of `modules`, roots then names in order."""
    names, modules = tuple(names), tuple(modules)
    for base in roots:
        for name in names:
            d = Path(base) / name / "scripts"
            if all((d / m).is_file() for m in modules):
                return d
    return None


def owner_routing(workspace: Path) -> dict:
    """The gateway's reading of the owner (`state/owner-routing.json`); {} when absent or unreadable."""
    try:
        d = json.loads(status_path("owner-routing.json", Path(workspace)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def owner_dm(routing: dict) -> str:
    return str(routing.get("owner_dm") or "").strip()


def agent_identity(routing: dict, environ=None) -> str:
    """The identity database writes are signed with: an IDENTITY_VARS variable wins over the routing's."""
    env = os.environ if environ is None else environ
    return next((env[v].strip() for v in IDENTITY_VARS if (env.get(v) or "").strip()), "") \
        or str(routing.get("identity") or "").strip()


def channel_credentials(cap, environ=None) -> dict:
    """The capability's credential variables from the AG2 Space channel env file the shared
    resolver picks; empty when a variable already names either, so the two never mix."""
    from channel_env_resolve import resolve_channel_env
    from channel_token import token_from_env_file
    from util_paths import claude_home_path
    env = os.environ if environ is None else environ
    names = (*getattr(cap, "URL_VARS", ()), *getattr(cap, "TOKEN_VARS", ()))
    if not names or any(env.get(v) for v in names):
        return {}
    path = resolve_channel_env(claude_home_path("channels"), "ag2space")
    if path is None:
        return {}
    found = {v: token_from_env_file(v, path) for v in names}
    return {v: x for v, x in found.items() if x}


def resolve_credentials(cap, collab_url: Optional[str]) -> tuple:
    """(url, token) by the capability's own order; when that finds neither, from the channel
    env file the shared resolver picks (a desktop install keeps them there)."""
    try:
        return cap.resolve_url(collab_url), cap.resolve_token(None)
    except Exception:
        # Never with --collab-url: that host was named elsewhere, and the channel token
        # must not go to a host its own file did not name (the capability's no-mix rule).
        extra = {} if collab_url else channel_credentials(cap)
        if not extra:
            raise
    os.environ.update(extra)
    return cap.resolve_url(None), cap.resolve_token(None)
