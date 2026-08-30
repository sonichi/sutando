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


def _declares_discord_id(ancestors: list, key: str, siblings=None) -> bool:
    """DISCORD evidence, FAIL CLOSED. Naming the referent is not naming the
    provider: `telegram_human_id` and `teams_human_id` name the human and
    someone else's account, and a denylist of providers cannot be complete.

    Evidence, in order: a sibling `provider` decides both ways when present;
    else the key says the whole word `discord`; else the one measured legacy
    spelling — a bare `id`/`ids` whose ancestor names the referent and which
    states no provider at all. Anything else is not a Discord id.
    """
    if isinstance(siblings, dict) and siblings.get("provider") is not None:
        return str(siblings["provider"]).strip().lower() == "discord"
    if "discord" in _field_words(key):
        return True
    return [w.lower() for w in _WORDS.findall(str(key))] in (["id"], ["ids"]) \
        and _typed_path(ancestors)


def _declared_provider(mapping) -> "str | None":
    """The `provider` this mapping states, lowercased, or None."""
    if isinstance(mapping, dict) and mapping.get("provider") is not None:
        return str(mapping["provider"]).strip().lower()
    return None


def _discord_source(ancestors: list, key: str, provider: "str | None") -> bool:
    """May a snowflake in THIS leaf be read as a Discord id? Governs mining and
    citation for every leaf, not only `*_id` ones — `display_name` under a
    referent was a documented non-evidence field and was being mined.

    An enclosing `provider` decides, at any depth; else the key says the whole
    word `discord`; else the legacy bare `id`/`ids` under a referent ancestor;
    else the leaf itself must name the referent (`stand_status`), because an
    ancestor naming it says nothing about which of its fields hold ids.
    """
    # A provider names the NAMESPACE, not that every sibling under it holds an
    # identity: `activity.rooms` and `display_name` are neither.
    if not (_id_slot(key) or _verdicts_from_field(key)
            or "discord" in _field_words(key)):
        return False
    if provider is not None:
        return provider == "discord"
    if "discord" in _field_words(key):
        return True
    if _id_slot(key):
        return [w.lower() for w in _WORDS.findall(str(key))] in (["id"], ["ids"]) \
            and _typed_path(ancestors)
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


def _slot_failures(value, slot: str, path: list, shapes: list) -> None:
    """Every PRESENT value in a declared id slot from which no id can be read.

    Empty containers are the slot being empty, not a bad value, so a v2 doc's
    own `[]` collections re-migrate untouched.
    """
    values = list(value) if slot == "plural" and isinstance(value, (list, tuple)) \
        else [value]
    for v in values:
        if _absent(v) or _snowflakes(json.dumps(v, default=str)):
            continue
        shapes.append({"path": ".".join(path), "kind": type(v).__name__,
                       "reason": "a field declaring an id holds a value no id "
                                 "can be read from, so the referent it states "
                                 "is discarded rather than absent"})


def _collect_ids(entry: dict, shapes: "list | None" = None) -> list:
    """Every discord-shaped id in the entry, in stable order.

    A leaf at a TYPED path that states a referent and yields no id is reported,
    never silently skipped — whatever type it holds.
    """
    found = []

    def walk(obj, path, provider):
        if isinstance(obj, dict):
            prov = _declared_provider(obj) or provider
            for k, v in obj.items():
                slot = _id_slot(k)
                # Inside the basis map the key IS the slot and the value prose.
                if slot and shapes is not None and ri.BASIS_FIELD not in path \
                        and _discord_source(path, str(k), prov):
                    _slot_failures(v, slot, path + [str(k)], shapes)
                walk(v, path + [str(k)], prov)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, path, provider)
        elif isinstance(obj, str) and path and ri.BASIS_FIELD not in path \
                and _discord_source(path[:-1], path[-1], provider):
            for sf in _snowflakes(obj):
                if sf not in found:
                    found.append(sf)
        elif obj is not None and not isinstance(obj, str) and _typed_path(path) \
                and shapes is not None and not _id_slot(path[-1] if path else ""):
            # A declared slot is reported by _slot_failures; without this guard
            # a non-string there is reported twice.
            shapes.append({"path": ".".join(path), "kind": type(obj).__name__,
                           "reason": "typed field holds a non-string value, so "
                                     "its id is unreadable rather than absent"})

    walk(entry, [], None)
    return found


def classify(key: str, entry: dict, triage_people: dict, peer_ids: dict,
             owner_id: str):
    """-> (human_id|None, stand_id|None, other_stands[], unresolved[], basis{},
    collisions[]) — collisions are axis clashes, reported with or without ids."""
    claims: dict = {}   # id -> {verdict -> [reasons]}
    bad: list = []      # not-id values; `states` is the referent each claimed
    collisions: list = []
    shape_failures: list = []

    def claim(id_, verdict, reason):
        claims.setdefault(id_, {}).setdefault(verdict, []).append(reason)

    for id_ in _collect_ids(entry, shape_failures):
        for field in _cited_in(entry, id_):
            for verdict, reason in _verdicts_from_field(field):
                claim(id_, verdict, reason)
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

    for id_ in list(claims):
        if id_ in peer_ids:
            claim(id_, STAND,
                  f"discord peers.json `{peer_ids[id_]}` (peer bot id)")
        if owner_id and id_ == owner_id:
            claim(id_, HUMAN, "discord-config.json `owner` (the human owner)")

    humans, stands, unresolved, basis = [], [], [], {}
    for id_, verdicts in claims.items():
        if len(verdicts) > 1:
            unresolved.append({
                "id": id_,
                "reason": "sources disagree on the referent",
                "claims": {v: r for v, r in verdicts.items()}})
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

    # The schema NAMES primacy — `stand_discord_id` is the primary agent and
    # `other_stand_discord_ids` the secondaries. Member order is not evidence.
    _primary = set(_snowflakes(json.dumps(entry.get(ri.STAND_FIELD) or "")))
    _secondary = set(_snowflakes(json.dumps(entry.get(ri.OTHER_STANDS_FIELD) or "")))
    stands.sort(key=lambda it: 0 if it[0] in _primary
                else 2 if it[0] in _secondary else 1)

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
