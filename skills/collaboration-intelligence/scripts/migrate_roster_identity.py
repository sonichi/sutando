#!/usr/bin/env python3
"""Migrate the v1 reviewer roster to the v2 named-referent schema.

Classification is EVIDENCE-ONLY. Every id placed in `human_discord_id` or
`stand_discord_id` is placed there because some source states which one it is:

  roster field names   `discord_human_id` / any field whose name contains
                       "human" that cites the id   -> human
                       any field whose name starts with "stand" or contains
                       "agent" that cites the id    -> agent (stand)
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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import roster_identity as ri  # noqa: E402

HUMAN, STAND = "human", "stand"


def _cited_in(entry: dict, id_: str) -> list:
    """Field names (dotted for nested) whose value mentions this id."""
    hits = []

    def walk(obj, prefix):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif isinstance(obj, list):
            for v in obj:
                walk(v, prefix)
        elif id_ and id_ in str(obj):
            hits.append(prefix)

    walk(entry, "")
    return hits


def _verdict_from_field(field: str):
    """A field name states the referent, or it states nothing."""
    tail = field.split(".")[-1].lower()
    whole = field.lower()
    if "human" in whole:
        return HUMAN, f"cited in `{field}` (field names the human)"
    if tail.startswith("stand") or whole.startswith("stand") or "agent" in whole:
        return STAND, f"cited in `{field}` (field names the agent)"
    return None, None


def _collect_ids(entry: dict) -> list:
    """Every discord-shaped id anywhere in the entry, in stable order."""
    found = []

    def walk(obj, key):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, str(k))
        elif isinstance(obj, list):
            for v in obj:
                walk(v, key)
        elif isinstance(obj, str) and "discord" in key.lower():
            if obj.isdigit() and len(obj) >= 17 and obj not in found:
                found.append(obj)

    walk(entry, "")
    return found


def classify(key: str, entry: dict, triage_people: dict, peer_ids: dict,
             owner_id: str):
    """-> (human_id|None, stand_id|None, other_stands[], unresolved[], basis{})"""
    claims: dict = {}   # id -> {verdict -> [reasons]}

    def claim(id_, verdict, reason):
        claims.setdefault(id_, {}).setdefault(verdict, []).append(reason)

    for id_ in _collect_ids(entry):
        for field in _cited_in(entry, id_):
            verdict, reason = _verdict_from_field(field)
            if verdict:
                claim(id_, verdict, reason)
        claims.setdefault(id_, {})

    tp = triage_people.get(key) or {}
    if tp.get("discord"):
        claim(str(tp["discord"]), HUMAN,
              f"pr-triage config `people.{key}.discord`")
    for bot in tp.get("bots") or []:
        claim(str(bot), STAND, f"pr-triage config `people.{key}.bots[]`")

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

    stand_id, others = None, []
    if stands:
        stand_id, reasons = stands[0]
        basis[ri.STAND_FIELD] = reasons
        others = [{"id": i, "basis": r} for i, r in stands[1:]]
    return human_id, stand_id, others, unresolved, basis


def migrate(doc: dict, triage_people: dict, peer_ids: dict, owner_id: str,
            source_name: str):
    out, rows = {}, []
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
    for key, entry in doc.items():
        if not ri.is_person_key(key) or not isinstance(entry, dict):
            out[key] = entry
            continue
        human, stand, others, unresolved, basis = classify(
            key, entry, triage_people, peer_ids, owner_id)
        new = dict(entry)                       # every provenance field survives
        new[ri.HUMAN_FIELD] = human
        new[ri.STAND_FIELD] = stand
        new["home_channel"] = entry.get("home_channel")
        if others:
            new[ri.OTHER_STANDS_FIELD] = others
        if unresolved:
            new[ri.UNRESOLVED_FIELD] = unresolved
        if basis:
            new[ri.BASIS_FIELD] = basis
        out[key] = new
        rows.append({
            "key": key,
            "before_discord_id": entry.get("discord_id"),
            "before_triage_human": (triage_people.get(key) or {}).get("discord"),
            "before_triage_bots": (triage_people.get(key) or {}).get("bots") or [],
            "after_human": human,
            "after_stand": stand,
            "after_other_stands": [o["id"] for o in others],
            "after_unresolved": [u["id"] for u in unresolved],
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
    triage_people = {}
    if a.triage_config and a.triage_config.is_file():
        triage_people = json.loads(a.triage_config.read_text()).get("people") or {}
    peer_ids = {}
    if a.peers and a.peers.is_file():
        peer_ids = {str(v): k for k, v in json.loads(a.peers.read_text()).items()}
    owner_id = ""
    if a.discord_config and a.discord_config.is_file():
        owner_id = str(json.loads(a.discord_config.read_text()).get("owner") or "")

    out, rows = migrate(doc, triage_people, peer_ids, owner_id, a.roster.name)
    dest = a.out or a.roster.with_suffix(".v2.json")
    if dest.resolve() == a.roster.resolve():
        print("refusing to overwrite the input roster", file=sys.stderr)
        return 2
    dest.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    if a.table:
        print_table(rows)
    print(f"\nwrote {dest} ({len(rows)} people); input untouched", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
