#!/usr/bin/env python3
"""Migrate the v1 reviewer roster to the v2 named-referent schema.

Classification is EVIDENCE-ONLY. Every id placed in `human_discord_id` or
`stand_discord_id` is placed there because some source states which one it is:

  roster field names   a field naming the WORD "human" (or "person") that
                       cites the id                 -> human
                       a field naming the WORD "stand" or "agent"
                       that cites the id            -> agent (stand)
                       Words, not substrings: `inhuman`, `agentless` and
                       `understanding` state nothing and resolve nothing.
  pr-triage config     `people.<login>.discord` -> human,
                       `people.<login>.bots[]`  -> stand (that file's schema
                       names the two separately, which is why it is usable)
  discord peers.json   every value is a peer BOT id -> stand
  discord-config.json  `owner` -> human

An id with no such statement, or with statements that disagree, goes to
`unresolved_discord_ids` with the reason. Guessing is the one failure this
migration must not commit: a wrong guess makes a person and their agent
interchangeable everywhere downstream.

The input file is never written. Output goes to --out (default: a sibling
`.v2.json`), and --table prints the per-entry before/after.

    python3 migrate_roster_identity.py --roster <v1.json> \
        [--triage-config <pr-triage/config.json>] [--peers <peers.json>] \
        [--discord-config <discord-config.json>] [--out <v2.json>] [--table]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import tempfile
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import roster_identity as ri  # noqa: E402

HUMAN, STAND = "human", "stand"

# The writer overwrites the malformed slot; this key carries the finding on.
SHAPE_FIELD = ri.SHAPE_FIELD    # owned by the schema module

_SNOWFLAKE = re.compile(r"(?<!\d)\d{17,20}(?!\d)")


def _snowflakes(text: str) -> list:
    """Every whole snowflake in a string, never a prefix of a longer one."""
    return _SNOWFLAKE.findall(str(text))


def _is_snowflake(v) -> bool:
    return isinstance(v, str) and bool(_SNOWFLAKE.fullmatch(v))


def _cited_in(entry: dict, id_: str) -> list:
    """Field names (dotted for nested) whose value mentions this id."""
    hits = []

    def walk(obj, path, provider):
        if isinstance(obj, dict):
            prov = _declared_provider(obj) or provider
            for k, v in obj.items():
                walk(v, path + [str(k)], prov)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, path, provider)
        elif id_ and id_ in _snowflakes(obj) and path \
                and ri.BASIS_FIELD not in path \
                and _discord_source(path[:-1], path[-1], provider):
                # whole-id match: a 17-digit id is a substring of an
                # 18-digit one, and that published the wrong referent.
            hits.append(".".join(path))

    walk(entry, [], None)
    return hits


_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")

_HUMAN_WORDS = frozenset({"human", "humans", "person"})
_STAND_WORDS = frozenset({"stand", "stands", "agent", "agents"})


def _field_words(field: str) -> frozenset:
    """Whole words in a field name, across `.`, `_` and camelCase.

    A SUBSTRING is not a statement: `inhuman`, `agentless` and `understanding`
    each contain one of these words and name none of these referents.
    """
    return frozenset(w.lower() for w in _WORDS.findall(str(field)))


def _verdicts_from_field(field: str) -> list:
    """EVERY referent a field name states — two when it names both.

    Returning both lets the existing disagreement path resolve it; picking one
    would be a precedence no source documents.
    """
    # Any word, at any depth: nesting must not change what a typed field
    # states, or `wrapper.stand_status.id` would classify as nothing.
    words = _field_words(field)
    out = []
    if words & _HUMAN_WORDS:
        out.append((HUMAN, f"cited in `{field}` (field names the human)"))
    if words & _STAND_WORDS:
        out.append((STAND, f"cited in `{field}` (field names the agent)"))
    return out


def _verdict_from_field(field: str):
    """The single referent a name states, or nothing. A name stating BOTH
    states neither on its own — callers deciding a slot must use the plural."""
    v = _verdicts_from_field(field)
    return v[0] if len(v) == 1 else (None, None)


def _bad(entries: list, value, states, reason: str, shapes: list) -> None:
    """Record a malformed observation against every canonical id it names.

    `str(container)` attaches the disagreement to a repr no reader can match,
    so the id it actually opposes keeps its slot.
    """
    found = _snowflakes(json.dumps(value, default=str))
    if not found:
        # `str(value)` here would publish a container repr into a field the
        # schema documents as ids only; record it as a shape failure instead.
        shapes.append({"path": None, "kind": type(value).__name__,
                       "reason": reason})
        return
    for sf in found:
        entries.append({"id": sf, "states": states, "reason": reason})


def _typed_path(path: list) -> bool:
    """True when ANY ancestor names the referent, not only the leaf key.

    `secondary_agent: {"id": ...}` states the referent at the ancestor; testing
    the leaf alone ("id") discards the very evidence the schema documents.
    """
    return any("discord" in seg.lower() or _verdicts_from_field(seg)
               for seg in path)


def _id_slot(field: str):
    """"singular" / "plural" for a name DECLARING it holds ids, else None.

    The last word decides, so `stand_status` stays free-form prose while
    `..._id` and `..._ids` are held to their declaration.
    """
    words = _WORDS.findall(str(field))
    last = words[-1].lower() if words else ""
    return "singular" if last == "id" else "plural" if last == "ids" else None


# The ancestors MEASURED to supply a referent for a bare `id` in this repo's
# rosters and tests. `telegram_human` names a person AND another provider.
_LEGACY_ID_ANCESTORS = frozenset({"human", "secondary_agent", "stand_status"})

# An identity leaf, positively: it names the referent, or it is the schema's
# own `user_id` / a bare `id`. `room_id` names a ROOM (`schema.md:67-70`).
def _identity_leaf(key: str) -> bool:
    words = [w.lower() for w in _WORDS.findall(str(key))]
    return bool(_verdicts_from_field(key)) or words in (["user", "id"], ["id"],
                                                        ["ids"])


def _declares_discord_id(ancestors: list, key: str, siblings=None) -> bool:
    """The same decision as `_discord_source`, with the provider read from this
    leaf's own mapping. Two functions deciding this drifted once already."""
    return _discord_source(ancestors, key, _declared_provider(siblings))


def _declared_provider(mapping) -> "str | None":
    """The `provider` this mapping states, lowercased, or None."""
    if isinstance(mapping, dict) and mapping.get("provider") is not None:
        return str(mapping["provider"]).strip().lower()
    return None


def _discord_source(ancestors: list, key: str, provider: "str | None") -> bool:
    """May a snowflake in THIS leaf be read as a Discord id? The ONE rule, used
    for mining, citation and slot validation alike.

    A provider names the NAMESPACE, not that every field under it is an
    identity — `activity.room_id` names a room. So the leaf must be identity-
    bearing first; then an enclosing `provider` decides at any depth; else the
    key says the whole word `discord`; else the legacy bare `id`/`ids` under a
    MEASURED ancestor, because any typed ancestor let `telegram_human.id`
    become Discord by moving the key one level down.
    """
    if not _identity_leaf(key) and "discord" not in _field_words(key):
        return False
    if provider is not None:
        return provider == "discord"
    if "discord" in _field_words(key):
        return True
    if _id_slot(key):
        return [w.lower() for w in _WORDS.findall(str(key))] in (["id"], ["ids"]) \
            and any(str(a).lower() in _LEGACY_ID_ANCESTORS
                    or "discord" in _field_words(a) for a in ancestors)
    return bool(_verdicts_from_field(key))


def _foreign_id_leaf(ancestors: list, key: str, siblings=None) -> bool:
    """A leaf declaring an id in someone else's namespace. Its digits are a
    PROVIDER id however Discord-shaped they look, so discovery must not mine
    them and no field may cite one — a wrong slot here names the wrong account.
    """
    return bool(_id_slot(key)) and not _declares_discord_id(ancestors, key,
                                                            siblings)


def _absent(value) -> bool:
    """None and blank agree with `roster_identity`'s readers, which coerce both
    to None; calling either malformed would split the two apart."""
    return value is None or (isinstance(value, str) and not value.strip())


def _slot_failures(value, slot: str, path: list, shapes: list, mines) -> None:
    """Every PRESENT member of a declared id slot the COLLECTOR does not read.

    `mines` IS the collector, so validation cannot drift from it: a value it
    accepts is one that reached a slot, and every other present member is a
    referent stated and then discarded. Empty containers are the slot being
    empty, so a v2 doc's own `[]` collections re-migrate untouched.
    """
    def _bad_shape(v):
        shapes.append({"path": ".".join(path), "kind": type(v).__name__,
                       "reason": "a field declaring an id holds a value no id "
                                 "can be read from, so the referent it states "
                                 "is discarded rather than absent"})

    def _leaves(v):
        # Lists keep the path in the collector, so a nested member is still IN
        # this slot; a mapping changes the key, so it answers as one value.
        if isinstance(v, (list, tuple)):
            for x in v:
                yield from _leaves(x)
        else:
            yield v

    values = list(_leaves(value)) if isinstance(value, (list, tuple)) \
        else [value]
    if slot == "singular":
        # From what the collector actually mines: a raw JSON scan counted an
        # unrelated snowflake in a record's metadata as a second slot id.
        seen = {sf for v in values for sf in mines(v)}
        if len(seen) > 1:
            # Order would decide; the schema says ONE id, so two resolve to NEITHER.
            _bad_shape(value)
            shapes[-1]["arbitrated_ids"] = sorted(seen)
            # FULL path, not the leaf: a leaf-only read says None, and None is
            # later treated as agreement with any other source.
            _v = _verdicts_from_field(".".join(path)) if path else []
            # The SET, not one value: `None` cannot mean both "states no
            # referent" and "states two", or agreement accepts either.
            shapes[-1]["arbitrated_states"] = sorted({v for v, _ in _v})
            return
    if not values:
        # An empty PLURAL slot is empty; an empty container in a SINGULAR one
        # is a present value the collector reads nothing from.
        if slot == "singular":
            _bad_shape(value)
        return
    for v in values:
        if _absent(v) or mines(v):
            continue
        _bad_shape(v)


def _collect_ids(entry: dict, shapes: "list | None" = None) -> list:
    """Every discord-shaped id in the entry, in stable order.

    A leaf at a TYPED path that states a referent and yields no id is reported,
    never silently skipped — whatever type it holds.
    """
    found = []

    def walk(obj, path, provider, sink, shapes):
        if isinstance(obj, dict):
            prov = _declared_provider(obj) or provider
            for k, v in obj.items():
                sub = path + [str(k)]
                slot = _id_slot(k)
                # Inside the basis map the key IS the slot and the value prose.
                if slot and shapes is not None and ri.BASIS_FIELD not in path \
                        and _discord_source(path, str(k), prov):
                    _slot_failures(v, slot, sub, shapes,
                                   lambda m, _p=sub, _pr=prov: _mines(m, _p, _pr))
                walk(v, sub, prov, sink, shapes)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, path, provider, sink, shapes)
        elif isinstance(obj, str) and path and ri.BASIS_FIELD not in path \
                and _discord_source(path[:-1], path[-1], provider):
            for sf in _snowflakes(obj):
                if sf not in sink:
                    sink.append(sf)
        elif obj is not None and not isinstance(obj, str) and path \
                and _discord_source(path[:-1], path[-1], provider) \
                and shapes is not None and not _id_slot(path[-1]):
            # A declared slot is reported by _slot_failures; without this guard
            # a non-string there is reported twice.
            shapes.append({"path": ".".join(path), "kind": type(obj).__name__,
                           "reason": "typed field holds a non-string value, so "
                                     "its id is unreadable rather than absent"})

    def _mines(member, path, provider) -> list:
        """The ids the collector reads from ONE member — the list, not a bool,
        so cardinality is counted from collection rather than re-derived."""
        scratch = []
        walk(member, path, provider, scratch, None)
        return scratch

    walk(entry, [], None, found, shapes)
    return found


def _slot_erased(entry, path) -> bool:
    """Is a writer-owned slot empty — so its earlier claim cannot be re-read?

    Absent, None and blank all mean the same thing here: the referent this slot
    once stated is no longer legible from the entry.
    """
    segs = str(path).split(".")

    def readable(node, i) -> bool:
        # A list does not consume a segment: `_cited_in` descends into members
        # on the same path, and an empty list states nothing.
        if isinstance(node, list):
            return any(readable(v, i) for v in node)
        if i == len(segs):
            return not (node is None or (isinstance(node, str) and not node.strip()))
        if not isinstance(node, dict) or segs[i] not in node:
            return False
        return readable(node[segs[i]], i + 1)

    return not readable(entry, 0)


def _canonical_seeds(seeds: list) -> list:
    """Deduped, ordered seeds. Re-seeding appends every pass, and an unbounded
    record would grow without bound while stating nothing new."""
    out, seen = [], set()
    for s in seeds:
        k = (s.get("path"), s.get("verdict"))
        if k in seen:
            continue
        seen.add(k)
        out.append({"path": s.get("path"), "verdict": s.get("verdict"),
                    "reason": s.get("reason")})
    return sorted(out, key=lambda s: (str(s["path"]), str(s["verdict"])))


def _carried_seeds(entry, arbitrated: set, fresh_claims: dict,
                   peer_ids: dict, owner_id: str):
    """Claims from a carried disagreement whose writer-owned slot we erased.

    A seed is discharged by REPAIR, not by OCCUPANCY. A slot reading again is
    sufficient only when nothing this pass still contradicts the seed: a RIVAL
    id filling the slot leaves the original disagreement untouched, so dropping
    the seed there republishes the contested id. Carry while this pass states a
    different referent for the same id; drop once nothing does.
    """
    if not isinstance(entry, dict):
        return
    for rec in entry.get(ri.UNRESOLVED_FIELD) or []:
        if not isinstance(rec, dict):
            continue
        id_ = rec.get("id")
        if not id_ or str(id_) in arbitrated:
            continue
        for seed in rec.get("seeded_by") or []:
            if not isinstance(seed, dict):
                continue
            path, verdict = seed.get("path"), seed.get("verdict")
            if not path or verdict not in (HUMAN, STAND):
                continue
            if not ri.writer_owned_path(path):
                continue
            # Every source, not just `claims`: peers.json and owner_id are
            # stamped later, so an id contested only there would look uncontested.
            states = set(fresh_claims.get(str(id_)) or {})
            if str(id_) in (peer_ids or {}):
                states.add(STAND)
            if owner_id and str(id_) == str(owner_id):
                states.add(HUMAN)
            contested = {v for v in states if v != verdict}
            if not _slot_erased(entry, path) and not contested:
                continue
            reason = seed.get("reason") or (
                f"cited in `{path}` before this migration overwrote it")
            yield str(id_), verdict, reason, {
                "path": path, "verdict": verdict, "reason": reason}


def _still_unresolved(entry, rec: dict, fresh_paths: set) -> bool:
    """Does a CARRIED finding still describe this entry?

    Kept when this pass re-found it, when the path cannot be re-checked, or
    when the value is gone (our own writer overwrites a canonical slot, so the
    evidence is absent rather than fixed). Dropped once the path reads cleanly
    again — otherwise a repair stays latched behind a stale record.
    """
    path = rec.get("path")
    if rec.get("kind") in (ri.INVALID_KIND, ri.OVERFLOW_KIND):
        # Pathless BY DESIGN and blocking: no path exists to re-check, so the
        # pathless-evidence rule below would silently discard the refusal.
        return True
    if not path:
        # Derived from a source this pass re-reads (the triage config), so the
        # fresh pass re-raises it if it still holds. Carrying it latches it.
        return False
    if path in fresh_paths:
        return True
    if ri.writer_owned_path(path):
        # Our own rewrite is not a repair: only re-migrating a repaired
        # SOURCE can clear a writer-owned finding.
        return True
    node = entry
    for seg in str(path).split("."):
        if not isinstance(node, dict) or seg not in node:
            return True                     # unreachable: cannot re-check
        node = node[seg]
    if node is None or (isinstance(node, str) and not node.strip()):
        return True                         # destroyed by the writer
    return not _mineable_now(node)


def _mineable_now(value) -> bool:
    """True when every present member of this value yields an id — the same
    readability the collector applies, asked of one corrected value."""
    vals = [value]
    while vals:
        v = vals.pop()
        if isinstance(v, (list, tuple)):
            if not v:
                return False
            vals.extend(v); continue
        if isinstance(v, str) and _snowflakes(v):
            continue
        if isinstance(v, dict) and _snowflakes(json.dumps(v, default=str)):
            continue
        return False
    return True


#: The backticked path inside a generated basis reason ("cited in `a.b`").
#: Reasons are produced by this module, so both sides share one spelling.
_CITED_PATH = re.compile(r"`([A-Za-z0-9_.]+)`")


def classify(key: str, entry: dict, triage_people: dict, peer_ids: dict,
             owner_id: str):
    """-> (human_id|None, stand_id|None, other_stands[], unresolved[], basis{},
    collisions[]) — collisions are axis clashes, reported with or without ids."""
    claims: dict = {}   # id -> {verdict -> [reasons]}
    bad: list = []      # not-id values; `states` is the referent each claimed
    collisions: list = []
    def claim(id_, verdict, reason):
        claims.setdefault(id_, {}).setdefault(verdict, []).append(reason)

    # THIS pass's own findings first; the carried ones are then reconciled
    # against them, so a repair can clear a record rather than latch forever.
    fresh: list = []
    collected = _collect_ids(entry, fresh)
    _has_carried = isinstance(entry, dict) and ri.SHAPE_FIELD in entry
    _raw_value = entry.get(ri.SHAPE_FIELD) if _has_carried else None
    # PRESENT but not a list. Coercing it to [] made corruption read as absence,
    # which is the one thing this field must never do.
    _bad_container = _has_carried and not isinstance(_raw_value, (list, tuple))
    _raw_carried = list(_raw_value) if isinstance(_raw_value, (list, tuple)) else []
    carried = ri.canonical_shape_failures(_raw_carried, bound=None)
    fresh_paths = {f.get("path") for f in fresh}
    # Unbounded for the decision: truncating before arbitration let member
    # order drop a live over-full-slot record and publish its id.
    shape_failures: list = ri.canonical_shape_failures(
        fresh + [c for c in carried if _still_unresolved(entry, c, fresh_paths)],
        bound=None)
    # A refusal that cannot be represented is still a refusal. Count only
    # REJECTED records: dedup also shrinks the list, and is not corruption.
    if _bad_container or any(ri.canonical_shape_failure(r) is None
                             for r in _raw_carried):
        shape_failures.append({
            "path": None, "kind": ri.INVALID_KIND,
            "reason": "carried refusal state was unusable and could not be "
                      "validated; treat as still-refused, not as absent"})
    # An id from an over-full singular slot is not evidence for that slot: it
    # would be picked by traversal order. Route it to unresolved instead.
    arbitrated = {i for f in shape_failures for i in f.get("arbitrated_ids") or []}
    # UNION, not last-wins: one id can appear in two failures stating different
    # referents, and a dict comprehension let member order decide which survived.
    arb_states: dict = {}
    for f in shape_failures:
        for i in f.get("arbitrated_ids") or []:
            arb_states.setdefault(i, set()).update(f.get("arbitrated_states") or [])
    arb_states = {i: sorted(v) for i, v in arb_states.items()}
    for id_ in sorted(arbitrated):   # a set's order must not reach the output
        _st = arb_states.get(id_) or []
        bad.append({"id": id_,
                    # One stated referent can agree with a matching slot; two
                    # cannot agree with either, and none states nothing.
                    "states": _st[0] if len(_st) == 1 else None,
                    "collision": len(_st) > 1,
                    "reason": "two ids claim one singular slot; order would "
                              "decide, so neither is taken"})
    seeds: dict = {}    # id -> writer-owned slots that stated a referent
    for id_ in [c for c in collected if c not in arbitrated]:
        for field in _cited_in(entry, id_):
            for verdict, reason in _verdicts_from_field(field):
                claim(id_, verdict, reason)
                if ri.writer_owned_path(field):
                    seeds.setdefault(id_, []).append(
                        {"path": field, "verdict": verdict, "reason": reason})
        claims.setdefault(id_, {})
    # A declared login is the SOLE join key, matched case-insensitively: the
    # local-key fallback crosses axes, and case-sensitivity splits one person.
    join = entry.get("github") or key
    hit = (triage_people or {}).get(str(join).casefold())
    tp, src = (hit[1] or {}, f"people.{hit[0]}") if hit else ({}, f"people.{join}")
    if str(join).casefold() != str(key).casefold() and \
            str(key).casefold() in (triage_people or {}):
        # Two axes collide on one spelling. Dropping the local-key row loses a
        # real identity silently; reject it here so the evidence survives.
        _hit = (triage_people or {}).get(str(key).casefold())
        _why = (f"pr-triage `people.{key}` collides with roster key `{key}`, "
                f"whose declared github is `{join}` — the two may name "
                "different people; resolve before promoting")
        # The collision is between two IDENTITY AXES, so it exists whether or
        # not the colliding row happens to carry an id to hang it on.
        collisions.append({"key": key, "join": join, "reason": _why})
        for sf in _snowflakes(json.dumps((_hit[1] if _hit else {}) or {})):
            bad.append({"id": sf, "states": None, "collision": True,
                        "reason": _why})
    # A typed field states the referent but not that the VALUE is an id. An
    # unvalidated one publishes junk into the slot the schema exists to protect.
    if "discord" in tp and not _is_snowflake(tp.get("discord")):
        _bad(bad, tp["discord"], HUMAN,
             f"pr-triage `{src}.discord` is not a snowflake", shapes=shape_failures)
    if _is_snowflake(tp.get("discord")):
        claim(str(tp["discord"]), HUMAN,
              f"pr-triage config `{src}.discord`")
    bots = tp.get("bots")
    if bots is not None and not isinstance(bots, (list, tuple)):
        # A dict iterates its KEYS, a string its characters. Require the shape
        # the schema documents rather than anything that happens to iterate.
        _bad(bad, bots, STAND, f"pr-triage `{src}.bots` is not a list", shapes=shape_failures)
        bots = []
    bots = bots or []
    for bot in bots:
        if _is_snowflake(str(bot)):
            claim(str(bot), STAND, f"pr-triage config `{src}.bots[]`")
        else:
            _bad(bad, bot, STAND,
                 f"pr-triage `{src}.bots[]` entry is not a snowflake",
                 shapes=shape_failures)

    # Below the triage read, not above it: the discharge test needs what THIS
    # pass states about the id, which does not exist until the config is read.
    for id_, verdict, reason, seed in _carried_seeds(
            entry, arbitrated, claims, peer_ids, owner_id):
        claim(id_, verdict, reason)
        seeds.setdefault(id_, []).append(seed)

    for id_ in list(claims):
        if id_ in peer_ids:
            claim(id_, STAND,
                  f"discord peers.json `{peer_ids[id_]}` (peer bot id)")
        if owner_id and id_ == owner_id:
            claim(id_, HUMAN, "discord-config.json `owner` (the human owner)")

    humans, stands, unresolved, basis = [], [], [], {}
    for id_, verdicts in claims.items():
        if len(verdicts) > 1:
            rec = {
                "id": id_,
                "reason": "sources disagree on the referent",
                "claims": {v: r for v, r in verdicts.items()}}
            if seeds.get(id_):
                rec["seeded_by"] = _canonical_seeds(seeds[id_])
            unresolved.append(rec)
        elif HUMAN in verdicts:
            humans.append((id_, verdicts[HUMAN]))
        elif STAND in verdicts:
            stands.append((id_, verdicts[STAND]))
        else:
            unresolved.append({
                "id": id_,
                "reason": "no source states whether this id is the person or "
                          "their agent; classify it from a source that names "
                          "the referent, never from the name it displays"})

    # Two ids both claiming the same slot is a conflict, not a pick.
    if len(humans) > 1:
        unresolved += [{"id": i, "reason": "two ids claim the human slot",
                        "claims": {HUMAN: r}} for i, r in humans]
        humans = []
    human_id = humans[0][0] if humans else None
    if human_id:
        basis[ri.HUMAN_FIELD] = humans[0][1]

    # The schema NAMES primacy, and the RANKING must read the same evidence the
    # collector did: scanning the raw slot text let a note's snowflake rank it.
    def _rank(item):
        roots = {p.split(".")[0] for r in (item[1] or [])
                 for p in _CITED_PATH.findall(str(r))}
        if ri.STAND_FIELD in roots:
            return 0
        return 2 if ri.OTHER_STANDS_FIELD in roots else 1

    stands.sort(key=_rank)

    stand_id, others = None, []
    if stands:
        stand_id, reasons = stands[0]
        basis[ri.STAND_FIELD] = reasons
        others = [{"id": i, "basis": r} for i, r in stands[1:]]

    # A NOTE only where it agrees with the slot the id holds; opposite-referent
    # evidence and alias collisions stay unresolved however they were spelled.
    slot = {}
    if human_id:
        slot[human_id] = HUMAN
    if stand_id:
        slot[stand_id] = STAND
    for o in others:
        slot.setdefault(o["id"], STAND)
    for b in bad:
        agrees = (b["id"] in slot and not b.get("collision")
                  and b.get("states") in (None, slot[b["id"]]))
        if agrees:
            basis.setdefault("malformed_observations", []).append(b)
            continue
        if b["id"] == human_id:
            human_id, humans = None, []
            basis.pop(ri.HUMAN_FIELD, None)
        if b["id"] == stand_id:
            stand_id = None
            basis.pop(ri.STAND_FIELD, None)
        others = [o for o in others if o["id"] != b["id"]]
        unresolved.append(b)
    resolved = {i for i in (human_id, stand_id) if i} | {o["id"] for o in others}
    assert not (resolved & {u["id"] for u in unresolved}), (
        "an id cannot be both resolved and unresolved")
    return (human_id, stand_id, others, unresolved, basis, collisions,
            shape_failures)


def _canonical_triage(triage_people: dict):
    """casefold -> (original key, value), plus the conflicting duplicates.

    GitHub logins are case-insensitive, so two spellings are one person; leaving
    both makes the second silently disappear behind the first.
    """
    canon, dupes = {}, []
    for k, v in (triage_people or {}).items():
        ck = str(k).casefold()
        if ck in canon and canon[ck][1] != v:
            dupes.append((canon[ck][0], k))
            continue
        canon.setdefault(ck, (k, v))
    return canon, dupes


def migrate(doc: dict, triage_people: dict, peer_ids: dict, owner_id: str,
            source_name: str):
    out, rows = {}, []
    canon, dupes = _canonical_triage(triage_people)
    if dupes:
        # Two spellings of one login carrying DIFFERENT records: nothing here
        # can choose between them, and picking one loses the other silently.
        raise ValueError(
            "pr-triage `people` has conflicting case-variant keys: "
            + "; ".join(f"{a!r} vs {b!r}" for a, b in dupes))
    out[ri.SCHEMA_KEY] = {
        "name": ri.SCHEMA_NAME,
        "version": ri.SCHEMA_VERSION,
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "migrated_from": source_name,
        "contract": (
            "`human_discord_id` is the person; `stand_discord_id` is the agent "
            "acting for them. Neither is ever filled from a v1 `discord_id`. "
            "An id no source classifies lives in `unresolved_discord_ids` and "
            "answers no lookup."),
    }
    aliased = {str(e.get("github") or k).casefold() for k, e in doc.items()
               if ri.is_person_key(k) and isinstance(e, dict)}
    known = {str(k).casefold() for k in doc}
    extra = {orig: {} for ck, (orig, _v) in canon.items()
             if ck not in known | aliased}
    for key, entry in list(doc.items()) + sorted(extra.items()):
        if key == ri.SCHEMA_KEY:
            continue                            # the destination schema is reserved
        if not ri.is_person_key(key) or not isinstance(entry, dict):
            out[key] = entry
            continue
        (human, stand, others, unresolved, basis, collisions,
         shape_failures) = classify(
            key, entry, canon, peer_ids, owner_id)
        new = dict(entry)                       # every provenance field survives
        new[ri.HUMAN_FIELD] = human
        new[ri.STAND_FIELD] = stand
        new["home_channel"] = entry.get("home_channel")
        # Assigned unconditionally: a rerun that only fills blanks leaves the
        # old value beside the new one — an id in human AND unresolved at once.
        new[ri.OTHER_STANDS_FIELD] = others
        new[ri.UNRESOLVED_FIELD] = unresolved
        new[ri.BASIS_FIELD] = basis
        # Carried, not recomputed: `new` no longer holds the malformed value.
        # Bounded only HERE — history, after every live finding is arbitrated.
        _sf = ri.canonical_shape_failures(shape_failures, bound=ri.SHAPE_MAX)
        if _sf:
            new[ri.SHAPE_FIELD] = _sf
        else:
            new.pop(ri.SHAPE_FIELD, None)
        out[key] = new
        # The MATCHED source, not the local key: reading triage_people[key] on
        # an aliased row printed "triage human = -" while filling after_human.
        _src = canon.get(str(entry.get("github") or key).casefold(), (None, {}))[1]
        rows.append({
            "key": key,
            "before_discord_id": entry.get("discord_id"),
            "before_triage_human": (_src or {}).get("discord"),
            "before_triage_bots": (_src or {}).get("bots") or [],
            "after_human": human,
            "after_stand": stand,
            "after_other_stands": [o["id"] for o in others],
            "after_unresolved": [u["id"] for u in unresolved],
            "after_collision": collisions,
            "after_shape_failure": shape_failures,
            "basis": basis,
        })
    return out, rows


def _fmt(v):
    return "-" if v in (None, "", [], {}) else (
        ",".join(map(str, v)) if isinstance(v, list) else str(v))


def print_table(rows):
    head = ("key", "v1 discord_id", "triage human", "triage bots",
            "-> human_discord_id", "-> stand_discord_id", "-> unresolved")
    data = [head] + [(r["key"], _fmt(r["before_discord_id"]),
                      _fmt(r["before_triage_human"]), _fmt(r["before_triage_bots"]),
                      _fmt(r["after_human"]),
                      _fmt([r["after_stand"]] + r["after_other_stands"]
                           if r["after_stand"] else r["after_other_stands"]),
                      _fmt(r["after_unresolved"])) for r in rows]
    w = [max(len(str(r[i])) for r in data) for i in range(len(head))]
    for n, r in enumerate(data):
        print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)).rstrip())
        if n == 0:
            print("  ".join("-" * x for x in w))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", required=True, type=Path)
    ap.add_argument("--triage-config", type=Path)
    ap.add_argument("--peers", type=Path)
    ap.add_argument("--discord-config", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--table", action="store_true")
    a = ap.parse_args()

    doc = json.loads(a.roster.read_text())

    def _source(flag, path):
        # An OMITTED source is a choice; a SUPPLIED one that is missing is an
        # error. Treating them alike migrates on evidence nobody knows is absent.
        if path is None:
            return None
        if not path.is_file():
            print(f"{flag} was supplied but does not exist: {path}", file=sys.stderr)
            raise SystemExit(2)
        return json.loads(path.read_text())

    triage_people = (_source("--triage-config", a.triage_config) or {}).get("people") or {}
    peer_raw = _source("--peers", a.peers) or {}
    peer_ids = {str(v): k for k, v in peer_raw.items()}
    owner_id = str((_source("--discord-config", a.discord_config) or {}).get("owner") or "")

    try:
        out, rows = migrate(doc, triage_people, peer_ids, owner_id, a.roster.name)
    except ValueError as exc:
        print(f"refusing to migrate: {exc}", file=sys.stderr)
        return 2
    dest = a.out or a.roster.with_suffix(".v2.json")
    if dest.resolve() == a.roster.resolve() or (
            dest.exists() and dest.samefile(a.roster)):
        # samefile too: a hardlink has a different resolved NAME and the same
        # inode, so a name check alone destroys the v1 rollback.
        print("refusing to overwrite the input roster", file=sys.stderr)
        return 2
    # A UNIQUE sibling created O_EXCL: a deterministic name can already be a
    # hardlink or symlink to the roster, and write_text follows it.
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=dest.name + ".",
                                    suffix=".tmp")
    tmp = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)     # never leave a half-written sibling
        raise
    if a.table:
        print_table(rows)
    print(f"\nwrote {dest} ({len(rows)} people); input untouched", file=sys.stderr)

    # A coverage gap is not a failure of the migration and not a success for the
    # caller: rc 5 says the file is written AND somebody in it is unaddressable.
    gaps = [r["key"] for r in rows
            if not r["after_human"] and not r["after_stand"]
            and not r["after_other_stands"]]
    unres = [r["key"] for r in rows if r["after_unresolved"]]
    # An axis collision blocks even when it carries no id: it is the two
    # referents that clash, not the values they happen to hold.
    coll = [r["key"] for r in rows if r.get("after_collision")]
    shape = [r["key"] for r in rows if r.get("after_shape_failure")]
    if gaps or unres or coll or shape:
        for k in gaps:
            print(f"  GAP {k}: no human and no stand id", file=sys.stderr)
        for k in unres:
            print(f"  GAP {k}: holds unresolved id(s)", file=sys.stderr)
        for k in coll:
            print(f"  COLLISION {k}: two identity axes name this key",
                  file=sys.stderr)
        for k in shape:
            print(f"  SHAPE {k}: a typed field holds a value no id can be read "
                  f"from", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
