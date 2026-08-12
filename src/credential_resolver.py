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

``voicePreference`` truth table (design 2b; amendment S1 — the SHARED
credential-source table this resolver, the TS twin, the supervisor
injection/``requires`` gate, ``startup-runtime.sh``'s shell gate,
``health-check.py``, and Rust status all implement;
``tests/voice-preference-consumers.test.sh`` pins agreement):

  - unset (legacy — every pre-preference install): managed(voice->text)->env.
  - 'managed': ONLY a non-quarantined managed entry satisfies the voice
    capability — a present env key must NOT silently satisfy a managed
    preference (the logout-quarantine bypass). No usable managed entry =>
    ``('', 'none')`` (fail actionably).
  - 'byok': the managed tier is skipped entirely for the voice capability
    (both fallback slots); only env keys satisfy.
  - ``quarantined: true`` (signed-out quarantine): every managed entry is
    treated as ABSENT in every mode and for every capability.

``voicePreference`` scopes the VOICE capability; 'gemini-text' resolution is
preference-independent but still honors the quarantine marker.

Managed-file schema (version 1):
  {"version": 1,
   "capabilities": {"gemini-voice": {"key": "...", "generation": "cg1-..."?}, ...},
   "voicePreference": "managed"|"byok"?, "quarantined": bool?,
   "preferenceRevision": u64?, "sessionRevision": u64?}
``preferenceRevision``/``sessionRevision`` are top-level coordination metadata
committed in the same atomic write as the policy fields (amendment R15); this
read side tolerates and ignores them. Malformed or unreadable files skip the
managed tier (empty caps, unset preference, not quarantined) — never raise.
``version`` is reserved for future schema changes and is NOT yet enforced
(matches the TS twin).
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
VoicePreference = str  # 'managed' | 'byok'


class ResolvedCredential(NamedTuple):
    key: str
    source: CredentialSource
    # S3: opaque Rust-minted generation (e.g. `cg1-<UUID>`) — REPORTED
    # verbatim, never minted or derived here. Legacy credentials omit it
    # (None), matching the TS twin's absent `credentialGeneration` field.
    credential_generation: Optional[str] = None


class _ManagedFile(NamedTuple):
    caps: dict
    voice_preference: Optional[VoicePreference]
    quarantined: bool


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


def _read_managed(path: Union[os.PathLike, str]) -> _ManagedFile:
    """Return the managed file's caps + policy fields, empty/unset on any problem.

    Never raises: missing/unreadable/malformed/wrong-shape all skip the tier.
    Mirrors the TS ``readManaged`` — a non-dict ``capabilities`` (list, etc.)
    yields {}; ``voicePreference`` outside the two literals reads as unset;
    ``quarantined`` is honored only as a strict JSON ``true`` (the only writer
    is the Rust host, which writes real booleans; whole-file corruption fails
    the parse and skips the managed tier anyway).
    """
    try:
        with open(path, encoding="utf-8") as fh:
            parsed = json.load(fh)
        if not isinstance(parsed, dict):
            return _ManagedFile({}, None, False)
        caps = parsed.get("capabilities")
        pref = parsed.get("voicePreference")
        return _ManagedFile(
            caps if isinstance(caps, dict) else {},
            pref if pref in ("managed", "byok") else None,
            parsed.get("quarantined") is True,
        )
    except (OSError, ValueError, TypeError):
        return _ManagedFile({}, None, False)


def resolve_credential(
    capability: Capability,
    managed_path: Optional[Union[os.PathLike, str]] = None,
) -> ResolvedCredential:
    """Resolve a capability to a credential + its source tier.

    Managed tier (walking the capability's fallback slots) wins over env; within
    each tier voice falls back to text. Tier order beats slot order — a managed
    TEXT key beats an env VOICE key. The S1 ``voicePreference``/``quarantined``
    truth table (module docstring) gates the tiers. Byte-identical to the TS
    twin.
    """
    slots = _CAPABILITY_FALLBACKS[capability]
    managed = _read_managed(managed_path if managed_path is not None else managed_credentials_path())
    # S1: the preference governs the VOICE capability; quarantine hides
    # managed entries from every capability in every mode.
    preference = managed.voice_preference if capability == "gemini-voice" else None
    if preference != "byok" and not managed.quarantined:
        for slot in slots:
            entry = managed.caps.get(slot)
            key = entry.get("key") if isinstance(entry, dict) else None
            if isinstance(key, str) and key:
                # S3: report the entry's opaque generation verbatim, when present.
                generation = entry.get("generation")
                return ResolvedCredential(
                    key=key,
                    source="managed",
                    credential_generation=generation
                    if isinstance(generation, str) and generation
                    else None,
                )
    if preference == "managed":
        # S1: ONLY a non-quarantined managed entry satisfies a managed
        # preference — a present env key must not silently satisfy it (the
        # logout-quarantine bypass the design closes). Fail actionably.
        return ResolvedCredential(key="", source="none")
    for slot in slots:
        key = os.environ.get(_ENV_VARS[slot])
        if key:
            # S3/U4: for the voice capability the launcher injects
            # SUTANDO_VOICE_CREDENTIAL_GENERATION beside a materialized BYOK
            # key. Manual/legacy .env keys stay generationless (Y4/Z4: a
            # generic vault write never carries a transactional generation).
            generation = (
                os.environ.get("SUTANDO_VOICE_CREDENTIAL_GENERATION")
                if capability == "gemini-voice"
                else None
            )
            return ResolvedCredential(
                key=key, source="env", credential_generation=generation or None
            )
    return ResolvedCredential(key="", source="none")


def credential_source_label(source: CredentialSource) -> str:
    """Map the internal source onto the design's 'managed'|'byok'|'none' vocabulary.

    Twin of the TS ``credentialSourceLabel`` (WS2 Step 3): surfaces say 'byok'
    where the resolver says 'env'. A mapper, not a rename, so existing 'env'
    consumers keep working unchanged.
    """
    if source == "managed":
        return "managed"
    if source == "env":
        return "byok"
    return "none"
