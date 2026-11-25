#!/usr/bin/env python3
"""Standing approvals: the owner's pre-approved classes of irreversible actions, kept as the
"Standing approvals" database in his room, read and written through the room capability.

  check --kind K [--attr key=value ...]   exit 0 and the row id when an approval covers it,
                                          1 when none does (with why), 2 when it cannot be read
  log-use --row R --action A --target T [--task ID]
                                          add one to Uses, stamp Last used, log a line on the row page
  propose --kind K --scope S [--expires D] [--note N] [--granted-by LINK]
                                          add a Proposed row; only the owner makes a row Active
  list [--json]                           counting, not counting, proposed and revoked rows
  ensure                                  create the database and any missing column

The room is STANDING_APPROVALS_ROOM (--room, env, then this skill's manifest), else the owner's
DM. The owner is STANDING_APPROVALS_OWNER by the same order, else the connection's own owner.
Which rows count is standing_approvals_policy.validity; anything unreadable is "not covered".
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
HERE = Path(__file__).resolve().parent
for _p in (REPO / "src", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from owner_room_access import (agent_identity, capability_scripts, owner_dm, owner_routing,  # noqa: E402
                               resolve_credentials)
import standing_approvals_policy as policy  # noqa: E402

# (skill directory, CLI module, client module), canonical layout first.
CAPABILITY_LAYOUTS = (("room-commons", "room_commons", "room_commons_client"),
                      ("room-collab", "room_collab", "room_collab_client"))
OPENERS = ("open_room_commons", "open_room_collab")
ROOM_KEY = "STANDING_APPROVALS_ROOM"
OWNER_KEY = "STANDING_APPROVALS_OWNER"
DB_NAME = "Standing approvals"
NEW_DB_ID = "standing_approvals"  # fixed, so two hosts creating it at once write the same keys
TIMEOUT_SEC = 90.0
SETTLE_SEC = 1.0
GAP = 1024
EXIT_COVERED, EXIT_NOT_COVERED, EXIT_ERROR = 0, 1, 2
SELECT_TYPES = ("select", "status")

STATUS_OPTIONS = [{"id": "proposed", "name": policy.PROPOSED, "color": "yellow", "group": "todo"},
                  {"id": "active", "name": policy.ACTIVE, "color": "green", "group": "doing"},
                  {"id": "revoked", "name": policy.REVOKED, "color": "gray", "group": "done"}]
# (logical id, column name, type, options); names are the contract, ids only name new columns.
COLUMNS = (
    ("kind", "Kind", "title", None),
    ("scope", "Scope", "text", None),
    ("note", "Note", "text", None),
    ("granted_by", "Granted by", "text", None),
    ("expires", "Expires", "date", None),
    ("status", "Status", "status", STATUS_OPTIONS),
    ("uses", "Uses", "number", None),
    ("last_used", "Last used", "text", None),
)
VIEWS = (("vtable", {"name": "All", "layout": "table"}),
         ("vboard", {"name": "By status", "layout": "board", "groupBy": "status"}))


class Unavailable(Exception):
    """The store cannot be reached or read; the message is the one line printed."""


# ---- config and target ------------------------------------------------------------

def manifest_config(key: str) -> str:
    try:
        cfg = json.loads((HERE.parent / "manifest.json").read_text(encoding="utf-8"))
        return str((cfg.get("config") or {}).get(key) or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def configured(key: str, cli: Optional[str], environ) -> str:
    """CLI, then env, then this skill's manifest `config` (skills/MANIFEST.md precedence)."""
    return (cli or "").strip() or (environ.get(key) or "").strip() or manifest_config(key)


def connection_owner(workspace: Path, room: str) -> str:
    """The owner of the connection that serves `room`, else of the primary connection."""
    try:
        import connections
        # None means a single-connection install; a refusal means no connection serves the room.
        row = connections.connection_for_room(room, workspace) or connections.primary_row(workspace) or {}
        return str(row.get("owner_mxid") or "").strip()
    except Exception:  # noqa: BLE001 — unknown owner, so nothing counts
        return ""


def target(workspace: Path, room_cli: Optional[str], owner_cli: Optional[str], environ) -> tuple:
    """(capability scripts dir, room, agent identity, owner identity); Unavailable when one is missing."""
    roots = (Path(workspace) / "skills", REPO / "skills")
    scripts = next((s for s in (capability_scripts(roots, (skill,), (f"{cli}.py", f"{client}.py"))
                                for skill, cli, client in CAPABILITY_LAYOUTS) if s), None)
    if scripts is None:
        raise Unavailable("no room capability installed (" + " or ".join(x[0] for x in CAPABILITY_LAYOUTS) + ")")
    routing = owner_routing(workspace)
    room = configured(ROOM_KEY, room_cli, environ) or owner_dm(routing)
    if not room:
        raise Unavailable(f"no room: {ROOM_KEY} is unset and no owner DM is known")
    user = agent_identity(routing, environ)
    if not user:
        raise Unavailable("no agent identity to sign database writes with")
    owner = configured(OWNER_KEY, owner_cli, environ) or connection_owner(workspace, room)
    if not owner:
        raise Unavailable(f"the owner's identity is unknown ({OWNER_KEY} is unset and no connection names one)")
    if owner == user:
        raise Unavailable("the owner identity equals the agent's own; refusing to treat agent writes as the owner's")
    return scripts, room, user, owner


def load_capability(scripts: Path):
    """(the CLI module, its open function, room_database) from the injected capability."""
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    _, cli, client = next(x for x in CAPABILITY_LAYOUTS if (Path(scripts) / f"{x[2]}.py").is_file())
    cap = importlib.import_module(cli)
    module = importlib.import_module(client)
    open_room = next((getattr(module, n) for n in OPENERS if callable(getattr(module, n, None))), None)
    if open_room is None:
        raise ImportError(f"{client} exports none of {', '.join(OPENERS)}")
    import room_database
    return cap, open_room, room_database


# ---- the database, as rows the policy reads -----------------------------------------

def find_db(rd, maps: dict) -> Optional[str]:
    hit = [d for d in rd.list_dbs(maps) if policy.fold(d["name"]) == policy.fold(DB_NAME)]
    if len(hit) > 1:
        raise Unavailable(f"{len(hit)} databases are named {DB_NAME!r} in this room; leave one")
    return hit[0]["id"] if hit else None


def columns(d: dict) -> dict:
    """{logical id: prop} for the columns this database has, matched by name."""
    out = {}
    for cid, name, _t, _o in COLUMNS:
        prop = next((p for p in d["props"] if policy.fold(p.get("name", "")) == policy.fold(name)), None)
        if prop is not None:
            out[cid] = prop
    return out


def _plain(prop: dict, v):
    if prop.get("type") in SELECT_TYPES:
        return next((o.get("name") for o in prop.get("options") or [] if o.get("id") == v), None)
    return v


def approvals(rd, maps: dict, db: str) -> list:
    d = rd.read_db(maps, db)
    cols = columns(d)
    out = []
    for r in d["rows"]:
        cells = {}
        for cid, prop in cols.items():
            c = rd.cell(d, r["id"], prop["id"])
            if c:
                cells[cid] = policy.Cell(_plain(prop, c.get("v")), c.get("by"), c.get("updated"))
        out.append(policy.Approval(r["id"], cells, created_by=r.get("by")))
    return out


def ensure_plan(rd, maps: dict, by: str, now_ms: int) -> tuple:
    """(db id, writes) creating the database and any missing column or status option; nothing
    that exists is rewritten except to append a missing option."""
    writes: dict = {}
    db = find_db(rd, maps)
    if db is None:
        if NEW_DB_ID in (maps.get("dbs") or {}):
            raise Unavailable(f"database id {NEW_DB_ID!r} is taken by another name")
        db = NEW_DB_ID
        writes["dbs"] = {db: {"name": DB_NAME, "order": (len(maps.get("dbs") or {}) + 1) * GAP,
                              "created": now_ms, "by": by}}
        writes["views"] = {rd.key(db, vid): {**v, "order": (i + 1) * GAP} for i, (vid, v) in enumerate(VIEWS)}
    d = rd.read_db({**maps, **{m: {**(maps.get(m) or {}), **w} for m, w in writes.items()}}, db)
    have, last = columns(d), max((p["order"] for p in d["props"]), default=0)
    has_title = any(p.get("type") == "title" for p in d["props"])
    for cid, name, typ, options in COLUMNS:
        prop = have.get(cid)
        if prop is None:
            last += GAP
            pid = cid if not any(p["id"] == cid for p in d["props"]) else rd.new_id("p")
            typ = "text" if typ == "title" and has_title else typ
            writes.setdefault("props", {})[rd.key(db, pid)] = {"name": name, "type": typ, "order": last,
                                                               **({"options": options} if options else {})}
        elif options and prop.get("type") in SELECT_TYPES:
            known = {policy.fold(o.get("name", "")) for o in prop.get("options") or []}
            missing = [o for o in options if policy.fold(o["name"]) not in known]
            if missing:
                writes.setdefault("props", {})[rd.key(db, prop["id"])] = {
                    **{k: v for k, v in prop.items() if k != "id"},
                    "options": list(prop.get("options") or []) + [{**o, "id": rd.new_id("o")} for o in missing]}
    return db, writes


def _merged(maps: dict, writes: dict) -> dict:
    out = {m: dict(maps.get(m) or {}) for m in ("dbs", "props", "rows", "cells", "views")}
    for m, entries in writes.items():
        for k, v in entries.items():
            if v is None:
                out[m].pop(k, None)
            else:
                out[m][k] = v
    return out


def cell_writes(rd, d: dict, row: str, values: dict, by: str, now_ms: int) -> dict:
    cols, out = columns(d), {}
    for cid, raw in values.items():
        prop = cols[cid]
        v = rd.normalize(prop, raw)
        if v is rd.UNFIT:
            raise Unavailable(f"{prop['name']}: {raw!r} does not fit this database's column")
        out[rd.key(d["id"], row, prop["id"])] = None if v is None else {"v": v, "updated": now_ms, "by": by}
    return out


def one_line(text: str, limit: int = 300) -> str:
    line = " ".join(str(text).split()).lstrip("#").strip()
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


# ---- commands -------------------------------------------------------------------------

async def with_doc(scripts: Path, room: str, fn):
    cap, open_room, rd = load_capability(scripts)
    url, token = resolve_credentials(cap, None, room)
    async with open_room(url, room, token, kind="db") as doc:
        return await fn(doc, rd)


def cmd_check(rows: list, kind: str, attrs: dict, owner: str) -> tuple:
    row, misses = policy.find_cover(rows, kind, attrs, owner, datetime.now())
    if row is not None:
        note = row.text("note").strip()
        lines = [row.id, f"covered by row {row.id} ({row.text('kind')}; scope: {one_line(row.text('scope')) or 'any'})"]
        if note:
            lines.append(f"note (not machine-checked): {one_line(note)}")
        return EXIT_COVERED, lines
    lines = [f"not covered: no counting standing approval for kind {kind!r}; ask the owner"]
    lines += [f"  {m}" for m in misses]
    return EXIT_NOT_COVERED, lines


def summarize(rows: list, owner: str) -> dict:
    now = datetime.now()
    out = {"counting": [], "not_counting": [], "proposed": [], "revoked": []}
    for r in rows:
        status = policy.fold(r.text("status"))
        item = {"row": r.id, "kind": r.text("kind"), "scope": r.text("scope"), "note": r.text("note"),
                "expires": (r.get("expires").value or {}).get("start") if isinstance(r.get("expires").value, dict)
                else r.text("expires"), "uses": r.get("uses").value or 0, "granted_by": r.text("granted_by")}
        if status == policy.fold(policy.PROPOSED):
            out["proposed"].append(item)
        elif status == policy.fold(policy.REVOKED):
            out["revoked"].append(item)
        else:
            ok, why = policy.validity(r, owner, now)
            (out["counting"] if ok else out["not_counting"]).append({**item, **({} if ok else {"why": why})})
    return out


def main(argv=None, environ=None) -> int:
    from workspace_default import resolve_workspace
    env = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(description="The owner's standing approvals, in his room's database.")
    ap.add_argument("--workspace", default=None, help="workspace dir (default: the resolved workspace)")
    ap.add_argument("--room", default=None, help=f"room id (default: {ROOM_KEY}, else the owner's DM)")
    ap.add_argument("--owner", default=None, help=f"owner account id (default: {OWNER_KEY}, else the connection's)")
    ap.add_argument("--timeout", type=float, default=TIMEOUT_SEC)
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--kind", required=True)
    c.add_argument("--attr", action="append", default=[], metavar="KEY=VALUE")
    u = sub.add_parser("log-use")
    u.add_argument("--row", required=True)
    u.add_argument("--action", required=True)
    u.add_argument("--target", required=True)
    u.add_argument("--task", default=None)
    p = sub.add_parser("propose")
    p.add_argument("--kind", required=True)
    p.add_argument("--scope", required=True, help="one `key: pattern` per line, or `any`")
    p.add_argument("--expires", default=None, help="YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    p.add_argument("--note", default=None)
    p.add_argument("--granted-by", dest="granted_by", default=None, help="link to the owner's message")
    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")
    sub.add_parser("ensure")
    args = ap.parse_args(argv)

    try:
        attrs = policy.parse_attrs(args.attr) if args.command == "check" else {}
        if args.command == "propose":
            policy.parse_scope(args.scope)
            if args.expires:
                policy.expiry_of(args.expires)
    except ValueError as e:
        print(f"standing-approvals: {e}")
        return EXIT_ERROR

    workspace = Path(args.workspace) if args.workspace else resolve_workspace(migrate=False)
    try:
        scripts, room, user, owner = target(workspace, args.room, args.owner, env)
    except Unavailable as e:
        if args.command == "check":
            print(f"not covered: the standing approvals cannot be read ({e}); ask the owner")
        else:
            print(f"standing-approvals: {e}")
        return EXIT_ERROR

    async def run(doc, rd):
        now_ms = int(time.time() * 1000)
        maps = doc.database
        if args.command in ("check", "list"):
            db = find_db(rd, maps)
            return approvals(rd, maps, db) if db else None
        if args.command in ("ensure", "propose"):
            db, writes = ensure_plan(rd, maps, user, now_ms)
            maps = _merged(maps, writes)
            d = rd.read_db(maps, db)
            if args.command == "ensure":
                if writes:
                    await doc.put_database(writes)
                    await doc.settle(SETTLE_SEC)
                return {"db": db, "written": sum(len(w) for w in writes.values())}
            row = rd.new_id("r")
            last = d["rows"][-1]["order"] if d["rows"] else 0
            writes.setdefault("rows", {})[rd.key(db, row)] = {"order": last + GAP, "created": now_ms, "by": user}
            values = {"kind": args.kind.strip(), "scope": args.scope.strip(), "status": policy.PROPOSED,
                      "note": args.note, "granted_by": args.granted_by, "uses": 0,
                      "expires": args.expires.replace(" ", "T") if args.expires else None}
            cells = cell_writes(rd, d, row, values, user, now_ms)
            writes.setdefault("cells", {}).update({k: v for k, v in cells.items() if v is not None})
            body = (f"Proposed by the agent on {time.strftime('%Y-%m-%d %H:%M')}. It does not count until "
                    f"the owner sets Status to {policy.ACTIVE}.\n\n# Uses\n\n")
            await doc.put_database(writes, bodies={rd.key(db, row): body})
            await doc.settle(SETTLE_SEC)
            return {"db": db, "row": row}
        # log-use
        db = find_db(rd, maps)
        if db is None:
            raise Unavailable(f"no {DB_NAME!r} database in this room")
        d = rd.read_db(maps, db)
        rows = {a.id: a for a in approvals(rd, maps, db)}
        if args.row not in rows:
            raise Unavailable(f"no row {args.row!r} in {DB_NAME!r}")
        a = rows[args.row]
        uses = a.get("uses").value
        uses = int(uses) if isinstance(uses, (int, float)) else 0
        stamp = time.strftime("%Y-%m-%d %H:%M")
        await doc.put_database({"cells": cell_writes(rd, d, args.row, {"uses": uses + 1, "last_used": stamp},
                                                     user, now_ms)})
        line = f"- {stamp} · {one_line(args.action)} · {one_line(args.target)}"
        line += f" · task {one_line(args.task, 80)}\n" if args.task else "\n"
        await doc.put_row_body(db, args.row, line, append=True)
        await doc.settle(SETTLE_SEC)
        ok, why = policy.validity(a, owner, datetime.now())
        return {"uses": uses + 1, "counting": ok, "why": why}

    try:
        result = asyncio.run(asyncio.wait_for(with_doc(scripts, room, run), args.timeout))
    except (Exception, SystemExit) as e:  # noqa: BLE001 — one line, never a raise into the caller
        if args.command == "check":
            print(f"not covered: the standing approvals cannot be read ({type(e).__name__}: {e}); ask the owner")
        else:
            print(f"standing-approvals: not done — {type(e).__name__}: {e}")
        return EXIT_ERROR

    if args.command == "check":
        if result is None:
            print(f"not covered: there is no {DB_NAME!r} database yet; ask the owner")
            return EXIT_NOT_COVERED
        code, lines = cmd_check(result, args.kind, attrs, owner)
        print("\n".join(lines))
        return code
    if args.command == "list":
        s = summarize(result or [], owner)
        if args.json:
            print(json.dumps(s, ensure_ascii=False, indent=2))
            return 0
        if result is None:
            print(f"no {DB_NAME!r} database yet (run `ensure`)")
        for section, items in s.items():
            print(f"{section.replace('_', ' ')}: {len(items)}")
            for it in items:
                extra = f" — {it['why']}" if it.get("why") else ""
                print(f"  {it['row']}  {it['kind']}  [{one_line(it['scope']) or 'any'}]  uses {it['uses']}{extra}")
        return 0
    if args.command == "ensure":
        print(f"standing-approvals: {DB_NAME!r} ready ({result['written']} write(s))")
    elif args.command == "propose":
        print(f"standing-approvals: proposed row {result['row']} — it counts only once the owner sets it Active")
    else:
        print(f"standing-approvals: logged use {result['uses']} on {args.row}"
              + ("" if result["counting"] else f" (note: the row no longer counts — {result['why']})"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
