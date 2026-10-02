#!/usr/bin/env python3
"""The room-database adapter for owner pending questions — the single reader and writer
core reaches through the manifest's `pending_questions_store` declaration.

`room_store(workspace)` is the discovery, kept at this edge: the room-collab
capability (the workspace's skill, else the repo's) and the owner's DM room and
agent identity from the gateway's own reading (`state/owner-routing.json`, an
`AG2SPACE_USER_ID` / `AG2_MATRIX_USER_ID` identity winning). It returns a
pending_questions_store.RoomDbStore whose client runs this file's `serve`, or
None and the reason when anything is missing.

`gather(workspace)` is every pass: reconcile (outbox replay, stale marks, the
transitional legacy ingest), then this host's open rows plus the outbox's held
questions, each once. `waiting`, `count` and `resolve` are its views.

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

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO / "src"))
from pending_questions_store import (DB_SCHEMA, INCOMPLETE, TERMINAL, RoomDbStore,  # noqa: E402
                                     ScriptDbClient, outbox_items, reconcile, safe_body, waiting_item)
from workspace_default import status_path  # noqa: E402

SKILL = "room-collab"
CLIENT_MODULE = "room_collab_client.py"
IDENTITY_VARS = ("AG2SPACE_USER_ID", "AG2_MATRIX_USER_ID")
GAP = 1024
SETTLE_SEC = 1.0
# The documented room-surface link shape; `page` is the database id.
LINK_TEMPLATE = "{origin}/#/room/{room}?surface=db&page={db}"
ROOM_KEY = "PENDING_QUESTIONS_ROOM"
# A collab service URL override, so a test run can point this adapter at an unreachable one.
URL_KEY = "PENDING_QUESTIONS_COLLAB_URL"


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


def configured_room(workspace: Path, environ) -> str:
    """The room holding the database: env, then this skill's manifest config, then
    this host's state/pending-questions-room; empty means the owner DM."""
    room = (environ.get(ROOM_KEY) or "").strip()
    if not room:
        try:
            cfg = json.loads((Path(__file__).resolve().parent.parent / "manifest.json").read_text(encoding="utf-8"))
            room = str((cfg.get("config") or {}).get(ROOM_KEY) or "").strip()
        except (OSError, ValueError, AttributeError):
            room = ""
    if not room:
        try:
            room = status_path("pending-questions-room", Path(workspace)).read_text(encoding="utf-8").strip()
        except OSError:
            room = ""
    return room


def _db_link(scripts: Path, room: str, url_override: str) -> Optional[str]:
    """The database's page in the room, from the capability's own URL resolution; None if unknown."""
    try:
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from room_collab import resolve_url  # noqa: PLC0415 — the injected capability
        return LINK_TEMPLATE.format(origin=resolve_url(url_override or None).rstrip("/"), room=room,
                                    db=DB_SCHEMA["id"])
    except Exception:  # noqa: BLE001 — a link is a convenience, never a precondition
        return None


def room_store(workspace: Path, environ=None, timeout: float = 90.0):
    """(RoomDbStore, where) when the capability, the room and an identity all
    resolve; (None, why not) otherwise. The room is the configured one, else the owner DM."""
    from util_paths import host_label
    env = os.environ if environ is None else environ
    scripts = skill_scripts(workspace)
    if scripts is None:
        return None, f"no {SKILL} capability installed"
    routing = owner_routing(workspace)
    shared = configured_room(workspace, env)
    room = shared or str(routing.get("owner_dm") or "").strip()
    if not room:
        return None, "no owner DM room known (state/owner-routing.json has no owner_dm)"
    user = next((env[v].strip() for v in IDENTITY_VARS if (env.get(v) or "").strip()), "") \
        or str(routing.get("identity") or "").strip()
    if not user:
        return None, "no agent identity to sign database writes with"
    url_override = (env.get(URL_KEY) or "").strip()
    argv = [sys.executable, str(Path(__file__).resolve()), "serve", "--room", room,
            "--user-id", user, "--skill-scripts", str(scripts)]
    if url_override:
        argv += ["--collab-url", url_override]
    return RoomDbStore(ScriptDbClient(argv, timeout), label=f"the owner's {'room' if shared else 'DM room'} {room}",
                       lock=status_path("pending-questions-db.lock", Path(workspace)), host=host_label(),
                       link=_db_link(scripts, room, url_override)), room


# ---- the reader -------------------------------------------------------------------

def gather(workspace: Path, environ=None) -> dict:
    """{"waiting": items, "done": n, "notes": [...], "store": where}: this host's open rows after
    the pass's reconcile, then the outbox's held questions (each marked not yet in the room);
    an ask id held in the outbox is listed once. Without a store: the outbox, and why."""
    from util_paths import host_label
    ws = Path(workspace)
    store, where = room_store(ws, environ)
    notes, rows, done = [], [], 0
    if store is None:
        notes.append(f"room database: not used ({where}); listing the local outbox only")
        return {"waiting": outbox_items(ws), "done": 0, "notes": notes, "store": None}
    try:
        rec = reconcile(store, ws, host_label())
        notes += [f"reconcile: FAILED — {e}" for e in rec["errors"]]
        for e in store.entries():
            if e["status"] in TERMINAL:
                done += 1
            elif not e["incomplete"]:
                rows.append(waiting_item(e["ask_id"], e["title"], e["body"], e["asked_at"], True, e["priority"]))
    except Exception as e:  # noqa: BLE001
        notes.append(f"ROOM DATABASE READ FAILED ({type(e).__name__}: {e}); listing the local outbox only")
        return {"waiting": outbox_items(ws), "done": 0, "notes": notes, "store": store.label}
    held = {r["ask_id"] for r in rows}
    rows += [it for it in outbox_items(ws) if it["ask_id"] not in held]
    return {"waiting": rows, "done": done, "notes": notes, "store": store.label}


def waiting(workspace: Path) -> list:
    return gather(workspace)["waiting"]


def count(workspace: Path) -> dict:
    g = gather(workspace)
    return {"open": len(g["waiting"]), "done": g["done"]}


def resolve(workspace: Path, ask_id: str, status: str) -> tuple:
    """(closed, message): the row's Closed cell takes `status` (Answered or Resolved); a held
    outbox question is filed first. Never reopens a closed row."""
    from util_paths import host_label
    ws = Path(workspace)
    store, where = room_store(ws)
    if store is None:
        return False, f"room database: not used ({where}); nothing to close"
    try:
        reconcile(store, ws, host_label())
        store.close(ask_id, status)
        return True, f"room database: {ask_id} -> {status} in {store.where(ask_id)}"
    except Exception as e:  # noqa: BLE001
        return False, f"room database: not changed — {type(e).__name__}: {e}"


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
    meta = (maps.get("rows") or {}).get(_key(db, row)) or {}
    return {"id": row, "cells": _cells(maps, db, row), "body": doc.row_body(db, row) or "",
            "created": meta.get("created") if isinstance(meta, dict) else None}


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
        mark = f"{INCOMPLETE}@{host}" if host else INCOMPLETE
        have = _cells(maps, db, row) if exists else {}
        resume = exists and have.get("recovery") == mark and have.get("host") == host
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
        if not exists or (doc.row_body(db, row) or "") == "":  # a body that already landed is kept
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
    raise ValueError(f"unknown op {op!r}")


async def _serve(args, req: dict) -> object:
    sys.path.insert(0, args.skill_scripts)
    from room_collab import resolve_token, resolve_url  # noqa: PLC0415 — the injected capability
    from room_collab_client import open_room_collab  # noqa: PLC0415
    url, token = resolve_url(args.collab_url or None), resolve_token(None)
    link = LINK_TEMPLATE.format(origin=url.rstrip("/"), room=args.room, db=req["schema"]["id"])
    async with open_room_collab(url, args.room, token, kind="db") as doc:
        result = await apply(doc, req, args.user_id, int(time.time() * 1000), link)
        await doc.settle(SETTLE_SEC)
        return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Serve one pending-questions room-database request.")
    ap.add_argument("command", choices=("serve",))
    ap.add_argument("--room", required=True)
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--skill-scripts", required=True)
    ap.add_argument("--collab-url", default=None)
    args = ap.parse_args(argv)
    try:
        req = json.loads(sys.stdin.read())
        result = asyncio.run(_serve(args, req))
    except Exception as e:  # noqa: BLE001 — the caller keeps the question in the outbox on any failure
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
