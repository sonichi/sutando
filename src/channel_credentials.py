"""Credential variables a room client reads, taken from the AG2 Space channel env file.

A desktop install keeps the agent's relay token and URL in the channel env file, which a skill's
own process may not have sourced. `channel_credentials(names)` returns the listed variables from
the file the shared resolver picks (`channel_env_resolve`), read through `channel_token`. It
returns nothing when the environment already names any of them, so a token never mixes with a
URL from another source. Stdlib only.
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
