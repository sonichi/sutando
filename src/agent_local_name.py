"""This agent's display name for text the owner reads, from local records only.

The tiers `agent_display_name.resolve()` shares, without its homeserver lookup and without the
channel credential files (a monitor that titles a card or a notice may touch neither):

1. the `agent_display_name` the gateway listed on the connection row for this agent's mxid
   (<workspace>/state/connections.json); the mxid is the caller's, else $AGENT_MXID, else the
   primary row's;
2. SUTANDO_DISPLAY_NAME, else AGENT_DISPLAY_NAME, from the environment;
3. a name made from the mxid's localpart.

The first tier that records anything wins; a name that is an identifier (a session name, an id)
is no name; then, and when nothing is recorded, the answer is `default`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping, Optional

from owner_text import DEFAULT_AGENT_NAME, named

NAME_MAX = 64
CONFIGURED_VARS = ("SUTANDO_DISPLAY_NAME", "AGENT_DISPLAY_NAME")
MXID = re.compile(r"^@([^:\s]+):([A-Za-z0-9.\-]+(?::\d+)?)$")


def clean(name: object) -> str:
    """`name` single-spaced and cut to NAME_MAX; '' for anything that is not a non-blank string."""
    return " ".join(name.split())[:NAME_MAX] if isinstance(name, str) else ""


def localpart_name(mxid: str) -> str:
    """A readable name from an mxid's localpart: `@blue-fox.agent:x` -> `Blue Fox`."""
    m = MXID.match(mxid or "")
    local = re.sub(r"\.agent$", "", m.group(1)) if m else ""
    words = [w for w in re.split(r"[-._=/]+", local) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)[:NAME_MAX]


def connection_name(mxid: str, workspace: Optional[Path] = None) -> str:
    """The display name the gateway listed for `mxid` on its connection row; '' when none."""
    if not mxid:
        return ""
    try:
        import connections
        rows = connections.read_rows(workspace)
    except Exception:  # noqa: BLE001 - an unreadable record leaves the next tier
        return ""
    return next((clean(r.get("agent_display_name")) for r in rows
                 if isinstance(r, dict) and r.get("agent_mxid") == mxid and clean(r.get("agent_display_name"))), "")


def _primary_mxid(workspace: Optional[Path]) -> str:
    try:
        import connections
        return str((connections.primary_row(workspace) or {}).get("agent_mxid") or "")
    except Exception:  # noqa: BLE001
        return ""


def local_name(default: str = DEFAULT_AGENT_NAME, *, workspace: Optional[Path] = None,
               environ: Optional[Mapping[str, str]] = None, mxid: Optional[str] = None) -> str:
    env = os.environ if environ is None else environ
    try:
        if mxid is None:
            mxid = (env.get("AGENT_MXID") or "").strip() or _primary_mxid(workspace)
        tiers = [connection_name(mxid, workspace)] + [env.get(v) or "" for v in CONFIGURED_VARS]
        tiers.append(localpart_name(mxid))
    except Exception:  # noqa: BLE001 - a name is never worth failing the notice it titles
        return default
    # The first recorded tier wins, as in resolve(); an identifier there is no name.
    return named(next((c for c in map(clean, tiers) if c), ""), default)
