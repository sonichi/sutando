#!/usr/bin/env python3
"""The room-database adapter for owner pending questions.

`room_store(workspace)` is the discovery, kept at this edge: the room-collab
capability (the workspace's skill, else the repo's) and the owner's DM room and
agent identity from the gateway's own reading (`state/owner-routing.json`, an
`AG2SPACE_USER_ID` / `AG2_MATRIX_USER_ID` identity winning). It returns a
pending_questions_store.RoomDbStore whose client runs this file's `serve`, or
None and the reason when anything is missing.

`register` records this adapter for the reminder pass of an install whose rows
were made before discovery recorded it.

`serve` answers one pending_questions_store.DbClient request (JSON on stdin) over
one connection to the room's databases document, writing only the DATABASE.md
shapes through the client's own put_database / put_row_body.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO / "src"))
from pending_questions_store import RoomDbStore, ScriptDbClient, register_adapter, safe_body  # noqa: E402
from workspace_default import status_path  # noqa: E402

SKILL = "room-collab"
CLIENT_MODULE = "room_collab_client.py"
IDENTITY_VARS = ("AG2SPACE_USER_ID", "AG2_MATRIX_USER_ID")
GAP = 1024
SETTLE_SEC = 1.0
# The documented room-surface link shape; `page` is the database id.
LINK_TEMPLATE = "{origin}/#/room/{room}?surface=db&page={db}"


def skill_scripts(workspace: Path) -> Optional[Path]:
    for base in (Path(workspace) / "skills" / SKILL, REPO / "skills" / SKILL):
        if (base / "scripts" / CLIENT_MODULE).is_file():
            return base / "scripts"
    return None


def owner_routing(workspace: Path) -> dict:
    try:
        d = json.loads(status_path("owner-routing.json", Path(workspace)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def room_store(workspace: Path, environ=None, timeout: float = 90.0):
    """(RoomDbStore, where) when the capability, the owner DM and an identity all
    resolve; (None, why not) otherwise."""
    from util_paths import host_label
    env = os.environ if environ is None else environ
    scripts = skill_scripts(workspace)
    if scripts is None:
        return None, f"no {SKILL} capability installed"
    routing = owner_routing(workspace)
    room = str(routing.get("owner_dm") or "").strip()
    if not room:
        return None, "no owner DM room known (state/owner-routing.json has no owner_dm)"
    user = next((env[v].strip() for v in IDENTITY_VARS if (env.get(v) or "").strip()), "") \
        or str(routing.get("identity") or "").strip()
    if not user:
        return None, "no agent identity to sign database writes with"
    argv = [sys.executable, str(Path(__file__).resolve()), "serve", "--room", room,
            "--user-id", user, "--skill-scripts", str(scripts)]
    try:
        register_adapter(workspace, Path(__file__))
    except OSError as e:
        print(f"pending_questions_room_db: could not register for the reminder ({e})", file=sys.stderr)
    return RoomDbStore(ScriptDbClient(argv, timeout), label=f"the owner's DM room {room}",
                       lock=status_path("pending-questions-db.lock", Path(workspace)), host=host_label()), room


# ---- serve: one request against the databases document -------------------------

def _key(*parts: str) -> str:
    return "|".join(parts)


def ensure_writes(maps: dict, schema: dict, by: str, now_ms: int) -> dict:
    """The database from `schema` if it is missing, and any of its properties,
    options or views that are missing; nothing that exists is changed."""
    db, writes = schema["id"], {m: {} for m in ("dbs", "props", "views")}
    if db not in (maps.get("dbs") or {}):
        writes["dbs"][db] = {"name": schema["name"], "order": (len(maps.get("dbs") or {}) + 1) * GAP,
                             "created": now_ms, "by": by}
    for i, p in enumerate(schema["props"]):
        k, have = _key(db, p["id"]), (maps.get("props") or {}).get(_key(db, p["id"]))
        if not isinstance(have, dict):
            writes["props"][k] = {**{x: y for x, y in p.items() if x != "id"}, "order": (i + 1) * GAP}
        elif p.get("options"):
            known = {o.get("id") for o in have.get("options") or [] if isinstance(o, dict)}
            missing = [o for o in p["options"] if o["id"] not in known]
            if missing:  # options are only ever appended; nothing stored is rewritten
                writes["props"][k] = {**have, "options": list(have.get("options") or []) + missing}
    for i, v in enumerate(schema["views"]):
        k, have = _key(db, v["id"]), (maps.get("views") or {}).get(_key(db, v["id"]))
        if not isinstance(have, dict):
            writes["views"][k] = {**{x: y for x, y in v.items() if x != "id"}, "order": (i + 1) * GAP}
        else:  # hidden props and filter clauses are only ever appended
            hide = [h for h in v.get("hidden") or [] if h not in (have.get("hidden") or [])]
            filt = [f for f in v.get("filter") or [] if f not in (have.get("filter") or [])]
            if hide or filt:
                writes["views"][k] = {**have, "hidden": list(have.get("hidden") or []) + hide,
                                      "filter": list(have.get("filter") or []) + filt}
    return {m: w for m, w in writes.items() if w}


def _cells(maps: dict, db: str, row: str) -> dict:
    pre = _key(db, row) + "|"
    return {k[len(pre):]: v.get("v") for k, v in (maps.get("cells") or {}).items()
            if k.startswith(pre) and isinstance(v, dict)}


def _row_json(doc, maps: dict, db: str, row: str) -> dict:
    return {"id": row, "cells": _cells(maps, db, row), "body": doc.row_body(db, row) or ""}


def _cell_writes(db: str, row: str, cells: dict, by: str, now_ms: int) -> dict:
    return {_key(db, row, p): ({"v": v, "updated": now_ms, "by": by} if v not in (None, "", []) else None)
            for p, v in cells.items()}


async def apply(doc, req: dict, by: str, now_ms: int, link: Optional[str] = None):
    """Run one DbClient request on an open databases document."""
    schema, op = req["schema"], req["op"]
    db = schema["id"]
    ensure = ensure_writes(doc.database, schema, by, now_ms)
    if ensure:
        await doc.put_database(ensure)
    maps = doc.database
    rows = maps.get("rows") or {}
    if op == "rows":
        mine = [k.split("|", 1)[1] for k in rows if k.startswith(db + "|")]
        return [_row_json(doc, maps, db, r) for r in mine]
    row = req["row"]
    exists = _key(db, row) in rows
    if op == "row":
        return _row_json(doc, maps, db, row) if exists else None
    if op == "add_row":
        # Row and body are two commits. The row is born marked incomplete (hidden, by its host), the
        # body follows, then the mark is cleared; a retry by the same host resumes a marked empty row.
        host = req["cells"].get("host")
        mark = f"incomplete@{host}" if host else "incomplete"
        have = _cells(maps, db, row) if exists else {}
        resume = exists and have.get("recovery") == mark and have.get("host") == host \
            and (doc.row_body(db, row) or "") == ""
        if exists and not resume:  # an existing row is never written here: its state is reported
            body = doc.row_body(db, row) or ""
            return {"created": False, "db": db, "row": row, "link": link,
                    "host": have.get("host"), "unsafe": safe_body(body) != body}
        if not exists:
            orders = [v.get("order") for k, v in rows.items() if k.startswith(db + "|") and isinstance(v, dict)
                      and isinstance(v.get("order"), (int, float))]
            await doc.put_database({"rows": {_key(db, row): {"order": (min(orders) - GAP) if orders else GAP,
                                                             "created": now_ms, "by": by}},
                                    "cells": _cell_writes(db, row, {**req["cells"], "recovery": mark}, by, now_ms)})
        await doc.put_row_body(db, row, req["body"])
        await doc.put_database({"cells": _cell_writes(db, row, {"recovery": None}, by, now_ms)})
        return {"created": True, "db": db, "row": row, "link": link}
    if not exists:
        raise LookupError(f"no row {row!r} in database {db!r}")
    if op == "set_cells":
        await doc.put_database({"cells": _cell_writes(db, row, req["cells"], by, now_ms)})
        return None
    if op == "set_body":
        await doc.put_row_body(db, row, req["body"])
        return None
    if op == "guarded":
        # A precondition on this replica's view, not a distributed lock: callers keep
        # writes whose merge order cannot matter (see pending_questions_store).
        have = _cells(maps, db, row)
        current = {p: have.get(p) for p in req["expect"]}
        if any(current[p] not in allowed for p, allowed in req["expect"].items()):
            return {"written": False, "current": current}
        await doc.put_database({"cells": _cell_writes(db, row, req["cells"], by, now_ms)})
        return {"written": True, "current": current}
    if op == "stamp":
        body, token = doc.row_body(db, row) or "", req["token"]
        n = body.count(token)
        if n != 1:
            raise ValueError(f"token {token!r} occurs {n} times, expected 1")
        await doc.put_row_body(db, row, body.replace(token, req["replacement"], 1))
        return None
    raise ValueError(f"unknown op {op!r}")


async def _serve(args, req: dict) -> object:
    sys.path.insert(0, args.skill_scripts)
    from room_collab import resolve_token, resolve_url  # noqa: PLC0415 — the injected capability
    from room_collab_client import open_room_collab  # noqa: PLC0415
    url, token = resolve_url(None), resolve_token(None)
    link = LINK_TEMPLATE.format(origin=url.rstrip("/"), room=args.room, db=req["schema"]["id"])
    async with open_room_collab(url, args.room, token, kind="db") as doc:
        result = await apply(doc, req, args.user_id, int(time.time() * 1000), link)
        await doc.settle(SETTLE_SEC)
        return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Serve one pending-questions room-database request.")
    ap.add_argument("command", choices=("serve", "register"))
    ap.add_argument("--room")
    ap.add_argument("--user-id")
    ap.add_argument("--skill-scripts")
    ap.add_argument("--workspace", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.command == "register":
        if args.workspace is None:
            from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
            args.workspace = resolve_workspace(migrate=False)
        store, where = room_store(args.workspace)
        print(f"registered for the reminder: {where}" if store else f"not registered: {where}")
        return 0 if store else 1
    if not (args.room and args.user_id and args.skill_scripts):
        ap.error("serve needs --room, --user-id and --skill-scripts")
    try:
        req = json.loads(sys.stdin.read())
        result = asyncio.run(_serve(args, req))
    except Exception as e:  # noqa: BLE001 — the caller falls back to the file on any failure
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
