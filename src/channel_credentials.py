"""Credential variables a room client reads, taken from the AG2 Space channel env file.

A desktop install keeps the agent's relay token and URL in the channel env file, which a skill's
own process may not have sourced. `channel_credentials(names)` returns the listed variables from
the file the shared resolver picks (`channel_env_resolve`), read through `channel_token`. It
returns nothing when the environment already names any of them, so a token never mixes with a
URL from another source. On an install with several connections, `room_env(...)` names the
credential of the connection serving a room instead (`connections.room_credential`). Stdlib only.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

CHANNEL = "ag2space"


def channel_credentials(names: Iterable[str], environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """{name: value} for each listed variable the channel env file sets; {} when the environment
    sets any of them, when `names` is empty, or when no file resolves."""
    from channel_env_resolve import resolve_channel_env
    from channel_token import token_from_env_file
    from util_paths import claude_home_path
    env = os.environ if environ is None else environ
    names = tuple(names)
    if not names or any(env.get(v) for v in names):
        return {}
    path = resolve_channel_env(claude_home_path("channels"), CHANNEL)
    if path is None:
        return {}
    found = {v: token_from_env_file(v, path) for v in names}
    return {v: x for v, x in found.items() if x}


def channel_value(name: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """One variable: the environment's value, else the channel env file's; '' when neither."""
    env = os.environ if environ is None else environ
    if env.get(name):
        return env[name]
    return channel_credentials((name,), env).get(name, "")


def room_env(url_vars: Iterable[str], token_vars: Iterable[str], room: Optional[str], *,
             environ: Optional[Mapping[str, str]] = None, workspace: Optional[Path] = None) -> Optional[Dict[str, str]]:
    """The credential variables for a client acting in `room` (None: the primary), from
    `connections.room_credential`: every listed variable blanked but the first token one, which
    holds the connection's `url|secret`. None keeps the caller's own resolution; the resolver's
    `NoConnectionForRoom` is raised, so another server's token is never used."""
    import connections
    token_vars = tuple(token_vars)
    got = connections.room_credential(room, workspace=workspace, environ=environ)
    if got is None or not token_vars:
        return None
    gateway_url, token = got
    if not connections.gateway_url_of(token):
        token = f"{gateway_url}|{token}"
    return {**{v: "" for v in (*url_vars, *token_vars)}, token_vars[0]: token}
