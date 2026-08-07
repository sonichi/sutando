"""Credential resolver — capability, not key (G8, desktop-parity plan).

Python twin of ``src/credential-resolver.ts`` (#2197). SAME contract, SAME
canaries: the desktop bundle runs both a TS surface (voice-agent.ts et al.)
and a Python core, so credential resolution must decide identically in both
languages. The shared test vectors (``tests/credential-resolver.test.py`` mirrors
``tests/credential-resolver.test.ts`` one-for-one) are what keep a latent defect
from surviving in only one twin (the policy-twin lesson from #2516).

Consumers ask for a CAPABILITY ('gemini-voice', 'gemini-text') and the resolver
decides which credential satisfies it, walking tiers in order:

  1. managed — desktop/AU-provisioned ``<workspace>/state/auth/managed-credentials.json``
               (per-host durable install state, same never-wiped contract as
               cloud-auth.json).
  2. env     — BYO keys from the environment (GEMINI_VOICE_API_KEY /
               GEMINI_API_KEY — today's behavior).

Within each tier a voice capability falls back to the text credential, mirroring
the existing GEMINI_VOICE_API_KEY -> GEMINI_API_KEY chain, so a single-key setup
keeps working unchanged at every tier. With no managed file present, resolution
is byte-for-byte identical to the legacy env chain — this module changes where
the decision lives, not what it decides. ``source`` surfaces WHICH tier satisfied
the capability so managed-vs-BYO drop-in is observable (Settings / health-check).

Managed-file schema (version 1):
  {"version": 1, "capabilities": {"gemini-voice": {"key": "..."}, ...}}
Malformed or unreadable files skip the managed tier — never raise. ``version`` is
reserved for future schema changes and is NOT yet enforced (matches the TS twin).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple, Optional, Union

from workspace_default import resolve_workspace

# Literal capability names (kept as plain strings for py3.8+ compatibility).
Capability = str  # 'gemini-voice' | 'gemini-text'
CredentialSource = str  # 'managed' | 'env' | 'none'


class ResolvedCredential(NamedTuple):
    key: str
    source: CredentialSource


# Per-capability lookup order within a tier (voice falls back to text).
_CAPABILITY_FALLBACKS = {
    "gemini-voice": ["gemini-voice", "gemini-text"],
    "gemini-text": ["gemini-text"],
}

# Env-var names per capability slot, in existing-chain order.
_ENV_VARS = {
    "gemini-voice": "GEMINI_VOICE_API_KEY",
    "gemini-text": "GEMINI_API_KEY",
}


def managed_credentials_path() -> Path:
    return resolve_workspace() / "state" / "auth" / "managed-credentials.json"


def _read_managed(path: Union[os.PathLike, str]) -> dict:
    """Return the managed file's ``capabilities`` mapping, or {} on any problem.

    Never raises: missing/unreadable/malformed/wrong-shape all skip the tier.
    Mirrors the TS ``readManaged`` — a non-dict ``capabilities`` (list, etc.)
    yields {}.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            parsed = json.load(fh)
        caps = parsed.get("capabilities") if isinstance(parsed, dict) else None
        return caps if isinstance(caps, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def resolve_credential(
    capability: Capability,
    managed_path: Optional[Union[os.PathLike, str]] = None,
) -> ResolvedCredential:
    """Resolve a capability to a credential + its source tier.

    Managed tier (walking the capability's fallback slots) wins over env; within
    each tier voice falls back to text. Tier order beats slot order — a managed
    TEXT key beats an env VOICE key. Byte-identical to the TS twin.
    """
    slots = _CAPABILITY_FALLBACKS[capability]
    managed = _read_managed(managed_path if managed_path is not None else managed_credentials_path())
    for slot in slots:
        entry = managed.get(slot)
        key = entry.get("key") if isinstance(entry, dict) else None
        if isinstance(key, str) and key:
            return ResolvedCredential(key=key, source="managed")
    for slot in slots:
        key = os.environ.get(_ENV_VARS[slot])
        if key:
            return ResolvedCredential(key=key, source="env")
    return ResolvedCredential(key="", source="none")
