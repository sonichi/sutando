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

#: Findings carried so a refusal survives a re-migration. Owned here because it
#: is part of the record, not a private detail of one script.
SHAPE_FIELD = "id_shape_failures"

#: A carried list is untrusted input: a hand-edit or an older writer can put
#: anything here, and this file is what says which shapes are usable.
SHAPE_MAX = 32

#: Fields the WRITER owns: a finding on one is unre-checkable from our own
#: output, so it is carried until the SOURCE is repaired and re-migrated.
WRITER_OWNED = (HUMAN_FIELD, STAND_FIELD, OTHER_STANDS_FIELD, UNRESOLVED_FIELD)


def writer_owned_path(path) -> bool:
    return isinstance(path, str) and path.split(".")[0] in WRITER_OWNED
_REFERENTS = ("human", "stand")


def _snowflake_list(value) -> list:
    """Whole snowflakes only. A bare string is NOT iterated — doing so wrote
    one fake id per character into `unresolved_discord_ids`."""
    if not isinstance(value, (list, tuple)):
        return []
    import re as _re
    return [v for v in value
            if isinstance(v, str) and _re.fullmatch(r"\d{17,20}", v)]


def canonical_shape_failure(rec) -> "dict | None":
    """One carried finding, canonicalised, or None when it is unusable.

    Canonical so an older writer's scalar `states` and this one's list collapse
    to a single record instead of two; None rather than an exception so a
    corrupt file degrades to "ignore this entry", never to a crash.
    """
    if not isinstance(rec, dict):
        return None
    reason = rec.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    path = rec.get("path")
    out = {"path": path if isinstance(path, str) else None,
           "kind": rec.get("kind") if isinstance(rec.get("kind"), str) else "?",
           "reason": reason}
    ids = _snowflake_list(rec.get("arbitrated_ids"))
    if ids:
        out["arbitrated_ids"] = sorted(set(ids))
    st = rec.get("arbitrated_states")
    st = [st] if isinstance(st, str) else st
    st = [v for v in st if v in _REFERENTS] if isinstance(st, (list, tuple)) else []
    if st:
        out["arbitrated_states"] = sorted(set(st))
    return out


#: An overflow aggregate stands in for arbitration records the bound cannot
#: hold. It keeps the ids and referents, so the slot stays refused.
OVERFLOW_KIND = "arbitration-overflow"
#: A carried refusal that could not be represented. Blocking, and pathless, so
#: it must survive both the bound and the pathless-evidence drop.
INVALID_KIND = "invalid"


def bears_identity(rec) -> bool:
    """Carries the fact the classifier actually reads: WHICH ids were contested.
    A referent with no id decides nothing, so it is diagnostic, not identity."""
    return bool(isinstance(rec, dict) and rec.get("arbitrated_ids"))


def must_keep(rec) -> bool:
    """Records the bound may never drop: identity facts and blocking refusals."""
    return bears_identity(rec) or (isinstance(rec, dict)
                                   and rec.get("kind") == INVALID_KIND)


def canonical_shape_failures(value, *, bound: "int | None" = SHAPE_MAX) -> list:
    """Canonicalised, de-duplicated, and bounded only when asked.

    `bound=None` for anything that FEEDS A DECISION — truncating live findings
    before classification let a dropped record publish an id.
    """
    if not isinstance(value, (list, tuple)):
        return []
    seen, out = set(), []
    for rec in value:
        c = canonical_shape_failure(rec)
        if c is None:
            continue
        import json as _json
        k = _json.dumps(c, sort_keys=True)
        if k not in seen:
            seen.add(k); out.append(c)
    if bound is None:
        return out
    # Diagnostic history is bounded; identity facts and blocking refusals are
    # not droppable, so an overflow AGGREGATES them rather than growing.
    keep = [r for r in out if must_keep(r)]
    rest = [r for r in out if not must_keep(r)]
    if len(keep) <= bound:
        return keep + rest[:max(0, bound - len(keep))]
    ids, states = set(), set()
    for r in keep:
        ids.update(r.get("arbitrated_ids") or [])
        states.update(r.get("arbitrated_states") or [])
    agg = {"path": None, "kind": OVERFLOW_KIND,
           "reason": f"{len(keep)} arbitration records exceeded the {bound}-record "
                     "bound; their ids and referents are aggregated here and "
                     "remain refused"}
    if ids:
        agg["arbitrated_ids"] = sorted(ids)
    if states:
        agg["arbitrated_states"] = sorted(states)
    return [agg]

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
