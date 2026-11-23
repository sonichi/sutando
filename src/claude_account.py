#!/usr/bin/env python3
"""Which Claude account a config dir is signed in to, as a non-reversible digest, and the
config dir the Sutando core runs under. Only non-secret identity fields are read; tokens never are.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional


def account_fingerprint(config_dir: Optional[str]) -> Optional[str]:
    """A non-reversible digest of the account the CLI is signed in to, else None.
    Only non-secret identity fields of `.claude.json` are read; tokens never are."""
    if not config_dir:
        return None
    try:
        acct = json.loads((Path(config_dir) / ".claude.json").read_text(encoding="utf-8")).get("oauthAccount")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(acct, dict):
        return None
    ident = [str(acct.get(k) or "") for k in ("accountUuid", "organizationUuid", "emailAddress")]
    if not any(ident):
        return None
    return hashlib.sha256("\0".join(ident).encode("utf-8")).hexdigest()[:16]


def account_key(config_dir: Optional[str]) -> str:
    """The account's fingerprint; without one, a digest of the config dir itself, so two
    unidentified logins never share a key. No config dir at all is `unscoped`, never the core."""
    fp = account_fingerprint(config_dir)
    if fp:
        return fp
    if not config_dir:
        return "unscoped"
    real = os.path.realpath(config_dir)
    return "dir-" + hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]


def core_config_dir() -> Optional[str]:
    """The config dir the core launcher resolves for the core, whatever this process runs under."""
    try:
        from sutando_config import resolve_claude_sutando_config_dir
        return str(resolve_claude_sutando_config_dir())
    except Exception:  # noqa: BLE001 — unknown, never a guess
        return None


def same_login(config_dir: Optional[str], other: Optional[str]) -> bool:
    """One config dir, or two signed in to the same identified account."""
    if not config_dir or not other:
        return False
    if os.path.realpath(config_dir) == os.path.realpath(other):
        return True
    mine = account_fingerprint(config_dir)
    return mine is not None and mine == account_fingerprint(other)
