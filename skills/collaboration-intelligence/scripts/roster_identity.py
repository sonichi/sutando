#!/usr/bin/env python3
"""One identity map, with the referent named in the field rather than inferred.

The v1 roster carried a single `discord_id` per person and the pr-triage config
carried `{discord, bots[]}` for the same people. Neither is a superset, and for
qingyun-wu the roster's `discord_id` held the AGENT id while pr-triage's
`discord` held the HUMAN. Any merge that trusts the shared field name therefore
produces a store in which a person and their agent are the same value.

v2 fixes that by naming the referent: `human_discord_id` and `stand_discord_id`
are separate fields, and an id nobody can classify from evidence goes into
`unresolved_discord_ids` rather than into either. Readers use the accessors
below and never fall back to `discord_id` — a v1 file must fail a v2 lookup, not
answer it approximately.
"""
from __future__ import annotations

SCHEMA_NAME = "reviewer-identity"
SCHEMA_VERSION = 2
SCHEMA_KEY = "_schema"

HUMAN_FIELD = "human_discord_id"
STAND_FIELD = "stand_discord_id"
OTHER_STANDS_FIELD = "other_stand_discord_ids"
UNRESOLVED_FIELD = "unresolved_discord_ids"
BASIS_FIELD = "id_basis"

#: A key starting with "_" is document metadata, not a person.
def is_person_key(key: str) -> bool:
    return not str(key).startswith("_")


def schema_version(doc: dict) -> int:
    """0 for a pre-migration document; never guess a version from its fields."""
    meta = doc.get(SCHEMA_KEY) if isinstance(doc, dict) else None
    if not isinstance(meta, dict) or meta.get("name") != SCHEMA_NAME:
        return 0
    v = meta.get("version")
    return v if isinstance(v, int) else 0


def require_v2(doc: dict, where: str = "roster") -> dict:
    """Refuse a pre-migration document. The v1 `discord_id` has no stated
    referent, so answering a human lookup from it is the defect this exists
    to prevent — an explicit refusal is the only safe read."""
    got = schema_version(doc)
    if got < SCHEMA_VERSION:
        raise ValueError(
            f"{where}: schema {SCHEMA_NAME}/{got or 'v1-unversioned'} — a v1 "
            f"`discord_id` does not state whether it is a person or an agent; "
            f"migrate to {SCHEMA_NAME}/{SCHEMA_VERSION} before looking ids up")
    return doc


def people(doc: dict) -> dict:
    return {k: v for k, v in doc.items()
            if is_person_key(k) and isinstance(v, dict)}


def human_discord_id(entry: dict):
    """The person. Never the agent, and never a v1 `discord_id`."""
    return (entry or {}).get(HUMAN_FIELD) or None


def stand_discord_id(entry: dict):
    """The agent that acts for the person."""
    return (entry or {}).get(STAND_FIELD) or None


def stand_discord_ids(entry: dict) -> list:
    """Primary stand first, then any secondary agents."""
    out = []
    primary = stand_discord_id(entry)
    if primary:
        out.append(primary)
    for extra in (entry or {}).get(OTHER_STANDS_FIELD) or []:
        eid = extra.get("id") if isinstance(extra, dict) else extra
        if eid and eid not in out:
            out.append(eid)
    return out


def unresolved_discord_ids(entry: dict) -> list:
    return list((entry or {}).get(UNRESOLVED_FIELD) or [])
