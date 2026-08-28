#!/usr/bin/env python3
"""Resolve a person/agent to the Stand you should actually address.

Exists because the rule "address the Stand, not the human" kept failing while
written down and loaded. A script makes the correct path the cheapest one.

FAILS LOUD, NEVER CLOSED: an unresolvable identity still prints what to do and
exits 0. Withholding a message is worse than misdirecting one, so this tool
never silently refuses -- it tells you exactly what it could not confirm.
"""
import argparse
import datetime
import pathlib
import re
import subprocess
import sys

def store() -> pathlib.Path:
    ws = subprocess.run(["bash", "scripts/sutando-config.sh", "workspace"],
                        capture_output=True, text=True).stdout.strip()
    return pathlib.Path(ws) / "data" / "collaboration-intelligence"

def load(d):
    # Either store may exist alone; a host with only reviewer-stands.json
    # must not crash here or sessions fall back to hand-rolled lookups.
    q, ents = {}, []
    yp = d / "quick-lookup.yaml"
    if yp.exists():
        import yaml
        q = yaml.safe_load(yp.read_text())["quick_lookup"]
        ep = d / "entities.yaml"
        if ep.exists():
            ents = yaml.safe_load(ep.read_text()).get("entities") or []
    return q, ents


def load_roster(d):
    """reviewer-stands.json rows, normalised to the quick-lookup row shape.

    The roster is keyed by short name with the GitHub login in the `github`
    FIELD; a key-equality lookup queries the wrong axis and reads a mapped
    reviewer as absent (measured twice: 2026-08-27, 2026-08-28 — both times
    `get("john-the-dev")` missed the entry keyed `rui`).
    """
    import json
    p = d / "reviewer-stands.json"
    if not p.exists():
        return []
    rows = []
    for key, r in json.loads(p.read_text()).items():
        if not isinstance(r, dict):
            continue
        rows.append({
            "entity_id": key,
            "agent_mxid": r.get("stand") or "",
            "github": r.get("github") or "",
            "human": r.get("human") or "",
            "allowlisted": r.get("allowlisted"),
            "one_line": f"github={r.get('github','')} human={r.get('human','')} stand={r.get('stand','')}",
        })
    return rows

def match(rows, needle, ents=()):
    n = needle.lower().lstrip("@")
    # an entity whose GitHub/slack/discord id matches, even if the name does not
    by_ident = {e.get("entity_id") for e in ents
                for i in (e.get("identities") or [])
                if n in str(i.get("provider_id", "")).lower()}
    # Identity matches OUTRANK role text and never mix with it: role text names
    # other people and repos, so a hit there is about the subject, not the person.
    strong = [r for r in rows
              if n in str(r.get("entity_id", "")).lower()
              or n in str(r.get("agent_mxid", "")).lower()
              or n == str(r.get("github", "")).lower()
              or n in str(r.get("human", "")).lower()
              or r.get("entity_id") in by_ident]
    if strong:
        return strong
    return [r for r in rows if n in str(r.get("one_line", "")).lower()]

def ids_for(ents, entity_id):
    for e in ents:
        if e.get("entity_id") == entity_id:
            return e.get("identities") or []
    return []

def main():
    ap = argparse.ArgumentParser(description="Look up who to address in the collaboration map.")
    ap.add_argument("query", nargs="?", help="name, handle, entity_id, or mxid")
    ap.add_argument("--all", action="store_true", help="list every known entity")
    a = ap.parse_args()

    d = store()
    if not d.exists():
        print(f"MAP MISSING at {d}\n  -> persistence unavailable. Send anyway; say the identity is unverified.")
        return 0
    q, ents = load(d)
    rows = (q.get("recent_entities") or []) + load_roster(d)
    if not rows:
        print(f"MAP EMPTY at {d} (no quick-lookup.yaml rows, no reviewer-stands.json)\n"
              "  -> persistence unavailable. Send anyway; say the identity is unverified.")
        return 0

    up = str(q.get("updated_at", "?"))
    try:
        age = (datetime.datetime.now(datetime.timezone.utc).date() - datetime.date.fromisoformat(up)).days
        stale = f"  map updated_at={up} ({age}d old, UTC){'  <-- STALE, treat hits as candidates' if age > 3 else ''}"
    except Exception:
        stale = f"  map updated_at={up}"

    if a.all or not a.query:
        print(f"KNOWN ENTITIES ({len(rows)})\n{stale}")
        for r in rows:
            st = r.get("agent_mxid") or "-- no Stand recorded --"
            print(f"  {r.get('entity_id',''):<28} {r.get('kind',''):<6} {st}")
        return 0

    hits = match(rows, a.query, ents)
    print(f"QUERY {a.query!r} -> {len(hits)} hit(s)\n{stale}")
    if not hits:
        print("  NO MATCH. Do NOT invent an id. Either ask the owner for the mapping, or send to a\n"
              "  channel you know and say plainly you could not resolve the Stand.")
        return 0

    for r in hits:
        eid = r.get("entity_id", "")
        stand = r.get("agent_mxid")
        print(f"\n  {eid}  [{r.get('kind','?')}]")
        print(f"    role: {str(r.get('one_line',''))[:150]}")
        if stand:
            print(f"    ADDRESS THIS -> {stand}")
        else:
            loose = re.findall(r"@[\w.\-]+", str(r.get("one_line", "")))
            if loose:
                print(f"    ⚠ NO STRUCTURED Stand, but the role text names: {', '.join(loose)}")
                print("      UNVERIFIED -- prose is not a resolved id. Use it, and say it is unconfirmed.")
            else:
                print("    ⚠ NO STAND RECORDED. Addressing the human id is NOT addressing their agent.")
                print("      Send, but state that the Stand is unresolved -- do not silently drop it.")
        rooms = r.get("active_rooms") or []
        print(f"    rooms: {', '.join(str(x) for x in rooms) if rooms else '-- none recorded --'}")
        idents = ids_for(ents, eid)
        if not idents:
            print("    ⚠ NO cross-platform ids in the store for this entity (discord/slack/github unknown).")
            print("      The SCRIPT cannot invent them -- the map was never given them.")
        for i in idents:
            print(f"    id: {i.get('provider')}={i.get('provider_id')} verified={i.get('verified')}")
        ev = str(r.get("evidence", ""))
        for kw in ("⚠", "Do NOT", "do NOT", "CORRECTED", "WRONG"):
            if kw in ev:
                k = ev.find(kw)
                print(f"    ⚠ WARNING IN MAP: {ev[k:k+190]}")
                break
    return 0

if __name__ == "__main__":
    sys.exit(main())
