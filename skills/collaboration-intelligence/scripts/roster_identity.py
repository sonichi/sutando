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

import re as _re

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


def path_join(segments) -> str:
    """The ONE spelling of an evidence path. A separator INSIDE a segment is
    escaped, so `path_split` returns the key the collector actually walked."""
    return ".".join(str(s).replace("\\", "\\\\").replace(".", "\\.")
                    for s in segments)


def path_split(path) -> list:
    """Inverse of `path_join`, and the reader of THIS codec's spelling only.

    A path off disk reaches it through `decoded_path`: an unescaped dot still
    separates, but a pre-codec path's backslash is literal, not an escape.
    """
    out, cur, esc = [], [], False
    for ch in str(path):
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ".":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if esc:
        cur.append("\\")
    out.append("".join(cur))
    return out


#: Names the codec that spelled a stored path. ABSENT is the discriminator for
#: the pre-codec writer: a bare `.` separator, and every backslash literal.
PATH_ENCODING_FIELD = "path_encoding"
PATH_ENCODING = "escaped"


def path_fields(segments) -> dict:
    """The fields that spell ONE evidence path: the string, plus the codec that
    wrote it WHEN escaping changed it.

    `a.b` is byte-identical under both codecs, so an unambiguous path keeps the
    pre-codec spelling and stays flag-free — the flag marks only the paths that
    could otherwise be read two ways.
    """
    segs = [str(s) for s in segments]
    enc = path_join(segs)
    out = {"path": enc}
    if enc != ".".join(segs):
        out[PATH_ENCODING_FIELD] = PATH_ENCODING
    return out


def decoded_path(rec):
    """A stored path in THIS codec's spelling, or None when it states none.

    A stored `\\.` is AMBIGUOUS across the two codecs — a literal backslash then
    the old separator, or an escaped dot — so the RECORD decides, never the
    string. Unflagged is re-encoded rather than re-parsed, so a path written
    before this codec still splits into the segments it always did.
    """
    path = rec.get("path") if isinstance(rec, dict) else None
    if not isinstance(path, str):
        return None
    if rec.get(PATH_ENCODING_FIELD) == PATH_ENCODING:
        return path
    return path_join(path.split("."))


def writer_owned_segments(segments) -> bool:
    """Is this path INSIDE a slot this writer owns? Ownership is the TOP-LEVEL
    key, so a schema spelling nested under a source object is that source's."""
    segs = list(segments)
    return bool(segs) and segs[0] in WRITER_OWNED


def writer_owned_path(path) -> bool:
    return isinstance(path, str) and writer_owned_segments(path_split(path))


def path_referent(path):
    """The referent a writer-owned PATH states, or None when it states none.

    `unresolved_discord_ids` is writer-owned but says by design which ids have
    NO agreed principal, so a verdict quoted against it is unbacked by the slot
    it names and cannot be checked against anything.
    """
    if not writer_owned_path(path):
        return None
    head = path_split(path)[0]
    if head == HUMAN_FIELD:
        return "human"
    if head in (STAND_FIELD, OTHER_STANDS_FIELD):
        return "stand"
    return None
_REFERENTS = ("human", "stand")


#: The ONE snowflake grammar. `[0-9]`, not `\d`: `\d` matches every Unicode
#: decimal digit, so a 19-char Arabic-Indic string travelled as an id.
SNOWFLAKE_PATTERN = r"[0-9]{17,20}"
_SNOWFLAKE_RE = _re.compile(SNOWFLAKE_PATTERN)


def is_snowflake(v) -> bool:
    """A snowflake is a STRING of digits. A JSON number is not one, and
    `str(v)` at a call site turns that check into a formatting step."""
    return isinstance(v, str) and bool(_SNOWFLAKE_RE.fullmatch(v))


def _snowflake_list(value) -> list:
    """Whole snowflakes only. A bare string is NOT iterated — doing so wrote
    one fake id per character into `unresolved_discord_ids`."""
    if not isinstance(value, (list, tuple)):
        return []
    return [v for v in value if is_snowflake(v)]


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
    # The ingest point for a carried path: every later reader gets THIS codec's
    # spelling, so no downstream split has to know which writer produced it.
    path = decoded_path(rec)
    out = {"path": path,
           "kind": rec.get("kind") if isinstance(rec.get("kind"), str) else "?",
           "reason": reason}
    if path is not None:
        out.update(path_fields(path_split(path)))
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
    """Records the bound may never drop: identity facts, blocking refusals, and
    the overflow marker itself — a marker a later write discards proves nothing."""
    return bears_identity(rec) or (isinstance(rec, dict) and rec.get("kind")
                                   in (INVALID_KIND, OVERFLOW_KIND))


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


def _unresolved_records(entry: dict):
    """`(records, ok)` for the unresolved container.

    `ok` is False when the field is present in a shape the schema does not
    define — a bare string, a mapping, a number. Those are not empty: iterating
    them yields characters, keys, or a TypeError, so the whole entry is treated
    as malformed rather than as having nothing contested.
    """
    raw = (entry or {}).get(UNRESOLVED_FIELD)
    if raw is None:
        return [], True
    if not isinstance(raw, (list, tuple)):
        return [], False
    for rec in raw:
        rid = rec.get("id") if isinstance(rec, dict) else rec
        if not is_snowflake(rid):
            return list(raw), False
    return list(raw), True


def _unresolved_id_set(entry: dict) -> set:
    """Ids the entry itself declines to resolve. Any of them answers no lookup."""
    recs, _ok = _unresolved_records(entry)
    out = set()
    for rec in recs:
        rid = rec.get("id") if isinstance(rec, dict) else rec
        if is_snowflake(rid):
            out.add(rid)
    return out


def entry_is_coherent(entry: dict) -> bool:
    """Is this ENTRY one identity record, rather than a set of fields that each
    look fine alone?

    Validating fields independently let one snowflake answer both
    `human_discord_id` and `stand_discord_id`, and let a malformed unresolved
    container read as "nothing contested". A role collision is unresolvable
    from inside the entry, so every accessor fails closed instead of picking.
    """
    if not isinstance(entry, dict):
        return False
    _recs, ok = _unresolved_records(entry)
    if not ok:
        return False
    human = entry.get(HUMAN_FIELD)
    stand = entry.get(STAND_FIELD)
    # A present canonical scalar is absent/None or a whole-string snowflake.
    # Filtering a malformed one out of the collision test below HIDES a collision.
    for role in (human, stand):
        if role is not None and not is_snowflake(role):
            return False
    if is_snowflake(human) and human == stand:
        return False
    # The container fails CLOSED on shape: skipping the loop for a non-list let
    # a mapping or bare string read as "nothing contested".
    extras = entry.get(OTHER_STANDS_FIELD)
    if extras is not None:
        if not isinstance(extras, (list, tuple)):
            return False
        for extra in extras:
            # Validated like the canonical scalars: the same id spelled `int` or
            # padded skipped the collision test below and read as a non-match.
            eid = extra.get("id") if isinstance(extra, dict) else extra
            if not is_snowflake(eid):
                return False
            if eid == human:
                return False
    return True


def _canonical_id(entry: dict, field: str):
    """The ONE validation every public accessor applies: the ENTRY is coherent,
    the value is a whole-string snowflake, and the entry does not list it as
    unresolved. The `_schema` marker says the document was migrated; it
    validates neither the value nor the record it sits in."""
    if not entry_is_coherent(entry):
        return None
    v = (entry or {}).get(field)
    if not is_snowflake(v) or v in _unresolved_id_set(entry):
        return None
    return v


def human_discord_id(entry: dict):
    """The person. Never the agent, and never a v1 `discord_id`."""
    return _canonical_id(entry, HUMAN_FIELD)


def stand_discord_id(entry: dict):
    """The agent that acts for the person."""
    return _canonical_id(entry, STAND_FIELD)


def stand_discord_ids(entry: dict) -> list:
    """Primary stand first, then any secondary agents.

    Shape-validating: the plural field is a LIST of snowflakes or of `{"id":
    ...}` records. Iterating anything else yielded one fake id per character
    for a bare string, and dict KEYS for a mapping.
    """
    if not entry_is_coherent(entry):
        return []
    out = []
    primary = stand_discord_id(entry)
    if primary:
        out.append(primary)
    extras = (entry or {}).get(OTHER_STANDS_FIELD)
    if not isinstance(extras, (list, tuple)):
        return out
    unresolved = _unresolved_id_set(entry)
    for extra in extras:
        eid = extra.get("id") if isinstance(extra, dict) else extra
        if is_snowflake(eid) and eid not in out and eid not in unresolved:
            out.append(eid)
    return out


def unresolved_discord_ids(entry: dict) -> list:
    """The contested records. A container the schema does not define answers
    with nothing rather than with its characters, its keys, or a TypeError."""
    recs, ok = _unresolved_records(entry)
    return recs if ok else []
