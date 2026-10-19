"""Relay gateway URL + bearer for in-session consumers, without trusting the environment.

The core session's environment does not carry relay credentials (the launcher
withholds AG2_REMOTE_* / REMOTE_TASK_*), so every consumer that runs inside it
resolves them here, in one order:

  1. the process environment (an exported value still wins);
  2. the channel env file, selected the way `scripts/channel-env.sh` selects it
     (`channel_env_resolve.resolve_channel_env`: launcher-named device env,
     then contained `channels/<source>/*.env` holding a non-empty token);
  3. the Keychain vault;
  4. the repo `.env` (`sutando_config.resolve_dotenv`), read from file.

Tiers 3 and 4 are lane-agnostic, so they serve only the primary source; a lane
never borrows the primary's credential. Nothing is ever written to os.environ.

Parsing and alias precedence are the canonical contract's
(`shared/ag2_gateway_credentials.py`); the per-file token rules (non-empty,
quote peeling) are `channel_token`'s. This module owns only the tier order.
Dependency-light: stdlib plus those siblings, so skills and core both import it.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Union

_SRC = Path(__file__).resolve().parent
for _p in (str(_SRC), str(_SRC.parent / "shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ag2_gateway_credentials import (  # noqa: E402
    TOKEN_ALIAS_PRECEDENCE, URL_ALIAS_PRECEDENCE, normalize_credentials,
    resolve_alias_precedence)
from channel_token import clean_for, token_from_env_file, token_from_vault  # noqa: E402

PRIMARY_SOURCE = "ag2space"

# A tier is a file to read, values already parsed, or None (tier skipped).
Tier = Union[Path, str, Mapping[str, str], None]
_UNSET: object = object()


@dataclass(frozen=True)
class RelayCredentials:
    """`raw` is the value as stored (possibly `url|secret`), for a child that
    parses it itself; `origin` is a secret-free tag of the tier that answered."""
    base_url: str
    token: str = field(repr=False, default="")
    raw: str = field(repr=False, default="")
    origin: str = "none"


def channel_env_file(source: str = PRIMARY_SOURCE, channels_dir=None) -> Optional[Path]:
    """The channel env file `scripts/channel-env.sh <source>` would pick, or None."""
    try:
        from channel_env_resolve import resolve_channel_env
        if channels_dir is None:
            from util_paths import claude_home_path
            channels_dir = claude_home_path("channels")
        return resolve_channel_env(channels_dir, source)
    except Exception:
        return None


def repo_env_file() -> Optional[Path]:
    """The repo `.env` startup.sh loads (workspace `.env` as its fallback), or None."""
    try:
        from sutando_config import resolve_dotenv
        p = Path(resolve_dotenv())
        return p if p.is_file() else None
    except Exception:
        return None


def vault_token(lookup: Optional[Callable[[str], str]] = None) -> str:
    """First non-empty alias in the vault; '' on any failure."""
    lookup = lookup or token_from_vault
    for var in TOKEN_ALIAS_PRECEDENCE:
        try:
            got = lookup(var)
        except Exception:
            got = ""
        if got:
            return got
    return ""


def _first(tier: Tier, names: Sequence[str]) -> str:
    """First non-empty `names` value in a tier; '' when absent or unreadable."""
    if tier is None:
        return ""
    for var in names:
        try:
            if isinstance(tier, Mapping):
                got = clean_for(var, tier.get(var, ""))
            else:
                got = token_from_env_file(var, Path(tier))
        except (OSError, UnicodeDecodeError):
            return ""
        if got:
            return got
    return ""


def resolve_relay_credentials(source: str = PRIMARY_SOURCE, *,
                              environ: Optional[Mapping[str, str]] = None,
                              env_token_names: Sequence[str] = TOKEN_ALIAS_PRECEDENCE,
                              channel: object = _UNSET,
                              vault: Optional[Callable[[], str]] = None,
                              repo: object = _UNSET) -> RelayCredentials:
    """Resolve the relay credential for `source` through the four tiers above.

    `channel` / `repo` default to the located files; pass a path, a parsed
    mapping, or None to skip. `vault` is a zero-arg getter (default: the vault).
    `base_url` is '' when nothing resolves; callers degrade on that.
    """
    environ = os.environ if environ is None else environ
    primary = source == PRIMARY_SOURCE
    if channel is _UNSET:
        channel = channel_env_file(source)
    if repo is _UNSET:
        repo = repo_env_file() if primary else None
    elif not primary:
        repo = None

    raw, name = resolve_alias_precedence(environ, env_token_names)
    raw = clean_for(name, raw) if raw else ""
    origin = f"env:{name}" if raw else "none"
    if not raw:
        raw = _first(channel, TOKEN_ALIAS_PRECEDENCE)
        origin = "channel-env" if raw else origin
    if not raw and primary:
        raw = (vault or vault_token)() or ""
        origin = "vault" if raw else origin
    if not raw:
        raw = _first(repo, TOKEN_ALIAS_PRECEDENCE)
        origin = "repo-env" if raw else origin

    explicit_url, _ = resolve_alias_precedence(environ, URL_ALIAS_PRECEDENCE)
    explicit_url = explicit_url.strip()
    if not explicit_url:
        explicit_url = _first(channel, URL_ALIAS_PRECEDENCE)
    # The repo URL only accompanies the repo token; it never re-points another tier's.
    fallback_url = _first(repo, URL_ALIAS_PRECEDENCE) if origin == "repo-env" else ""
    creds = normalize_credentials(raw, explicit_url=explicit_url,
                                  fallback_url=fallback_url, source=origin)
    return RelayCredentials(creds.base_url, creds.token, raw if creds.token else "",
                            creds.source)
