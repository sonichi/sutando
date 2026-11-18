"""The ag2-sparrow adapters' gateway credential I/O: the readers they hand the discovery owner.

gateway_credential_sources decides and opens nothing; these are the reads it is given. The bridge
uses read_file and vault_lookup for its own discovery; register_candidates is the masking-only
path for an adapter that shows text but decides nothing (the Hatch install diff): it registers every
candidate the environment leads to and drops the winners, notes and fatal, which the bridge decides
for itself when it starts. A refused file yields no values.
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Mapping, Optional

from . import gateway_credential_sources, gateway_credentials
from .bounded_file_read import read_regular


def read_file(path: str):
    """A credential file's (text, refusal reason): the one bounded reader."""
    return read_regular(path)


def channel_env_files(env: Mapping[str, str]) -> "gateway_credential_sources.EnvFiles":
    """The channel .env files a bridge given `env` reads, read by read_file, with any refusals."""
    return gateway_credential_sources.env_files(env, read_file)


def monorepo_src(marker: str, start: str = __file__) -> str:
    """The monorepo `src/` that holds `marker`, walking up from `start`; '' when ag2-sparrow
    runs standalone (pyproject install, no monorepo around it)."""
    cur = os.path.dirname(os.path.abspath(start))
    while True:
        if os.path.isfile(os.path.join(cur, "src", marker)):
            return os.path.join(cur, "src")
        parent = os.path.dirname(cur)
        if parent == cur:
            return ""
        cur = parent


def vault_lookup(src: str, vault_get=None) -> "Optional[Callable[[str], str]]":
    """The vault read for the ag2space token through the shared core `channel_token` in `src`
    (monorepo_src's answer): total-failure-safe, never surfacing the value; None without one."""
    try:
        if not src:
            return None
        if src not in sys.path:
            sys.path.insert(0, src)
        from channel_token import token_from_vault
    except Exception:
        return None
    return lambda key: token_from_vault(key, vault_get=vault_get)


def _vault() -> "Optional[Callable[[str], str]]":
    return vault_lookup(monorepo_src("channel_token.py"))


def register_candidates(env: Mapping[str, str], registry: "gateway_credentials.SecretRegistry") -> None:
    """Register every gateway credential candidate `env` leads to (the environment, the channel
    .env files, the token file, the vault when no earlier tier holds a token), for masking only.
    The vault reader is resolved once, and only when that tier is reached."""
    resolved: list = []

    def _vault_once(key: str) -> str:
        if not resolved:
            resolved.append(_vault() or (lambda _k: ""))
        return resolved[0](key)
    gateway_credential_sources.discover(
        env, gateway_credential_sources.env_files(env, read_file), read_token_file=read_file,
        vault_lookup=_vault_once, registry=registry)
