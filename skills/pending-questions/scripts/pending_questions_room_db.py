#!/usr/bin/env python3
"""The room-database adapter for owner pending questions — the single reader and writer
core reaches through the manifest's `pending_questions_store` declaration.

`room_store(workspace)` is the discovery, kept at this edge: the room capability (the
`room-commons` skill, else its `room-collab` alias, in the workspace's skills then the
repo's) and the owner's DM room and agent identity from the gateway's own reading
(`state/owner-routing.json`, an `AG2SPACE_USER_ID` / `AG2_MATRIX_USER_ID` identity
winning). It returns a pending_questions_store.RoomDbStore whose client runs this
file's `serve`, or None and the reason when anything is missing.

`gather(workspace)` is READ-ONLY: this host's open rows plus the outbox's held questions,
each once, with `unavailable: True` and `done: None` when the room cannot be read — never a
zero. `reconcile_pass(workspace)` is the explicit pass (outbox replay, local closes, stale marks,
the transitional legacy ingest); `ask_owner` runs it first, then core's queue, then the
row, and writes no row when the outbox could not hold the question. `resolve` closes the
row, else records the closure locally for the next reconcile. `remind` is the reminder.

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
HERE = Path(__file__).resolve().parent
for _p in (REPO / "src", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import pending_questions_ask as core_ask
from pending_questions_outbox import (ROOM_INTRODUCED, Outbox as HeldRecords, local_done_count,
                                      mark_room_used, room_was_used)
from pending_questions_store import (DB_SCHEMA, INCOMPLETE, TERMINAL, GuardFailed, Outbox, Question,
                                     RoomDbStore, ScriptDbClient, outbox_items, reconcile_pending, safe_body,
                                     waiting_item, write_question)
from workspace_default import status_path

# The skill directories that provide the room capability, canonical name first.
CAPABILITY_SKILLS = ("room-commons", "room-collab")
CLIENT_MODULE = "room_collab_client.py"
CLI_MODULE = "room_collab.py"
IDENTITY_VARS = ("AG2SPACE_USER_ID", "AG2_MATRIX_USER_ID")
GAP = 1024
SETTLE_SEC = 1.0
# The documented room-surface link shape; `page` is the database id.
LINK_TEMPLATE = "{origin}/#/room/{room}?surface=db&page={db}"
ROOM_KEY = "PENDING_QUESTIONS_ROOM"
# A collab service URL override, so a test run can point this adapter at an unreachable one.
URL_KEY = "PENDING_QUESTIONS_COLLAB_URL"


def skill_scripts(workspace: Path) -> Optional[Path]:
    for base in (Path(workspace) / "skills", REPO / "skills"):
        for name in CAPABILITY_SKILLS:
            d = base / name / "scripts"
            if (d / CLIENT_MODULE).is_file() and (d / CLI_MODULE).is_file():
                return d
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
            cfg = json.loads((HERE.parent / "manifest.json").read_text(encoding="utf-8"))
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
        return None, f"no room capability installed ({' or '.join(CAPABILITY_SKILLS)})"
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

def _unavailable(ws: Path, reason: str, store) -> dict:
    return {"waiting": outbox_items(ws), "done": None, "unavailable": True, "reason": reason,
            "link": getattr(store, "link", None), "store": getattr(store, "label", None),
            "notes": [f"ROOM DATABASE UNAVAILABLE ({reason}); the count is unknown"]}


def reconcile_pass(workspace: Path, environ=None) -> dict:
    """The explicit pass: {"flushed", "closed", "moved", "errors"}; without a store, one error."""
    from util_paths import host_label
    ws = Path(workspace)
    store, where = room_store(ws, environ)
    if store is None:
        return {"flushed": [], "closed": [], "moved": [], "errors": [f"room database: not used ({where})"]}
    try:
        return reconcile_pending(store, ws, host_label())
    except Exception as e:  # noqa: BLE001
        return {"flushed": [], "closed": [], "moved": [], "errors": [f"{type(e).__name__}: {e}"]}


def gather(workspace: Path, environ=None, reconcile: bool = False) -> dict:
    """{"waiting", "done", "unavailable", "reason", "link", "notes", "store"}: this host's open
    rows then the outbox's held questions (each marked not yet in the room), an ask id listed
    once. Read-only unless `reconcile`. Without a store: the outbox, and why — an outage
    (`unavailable`) when a room store was used before, a measurement when none ever was."""
    ws = Path(workspace)
    store, where = room_store(ws, environ)
    if store is None:
        if room_was_used(ws):
            return _unavailable(ws, f"room database not reachable ({where}), though it was used before", None)
        return {"waiting": outbox_items(ws), "done": local_done_count(ws), "unavailable": False, "reason": None,
                "link": None, "store": None, "notes": [f"room database: not used ({where}); listing the local outbox only"]}
    notes, rows, done = [], [], 0
    try:
        if reconcile:
            from util_paths import host_label  # noqa: PLC0415
            rec = reconcile_pending(store, ws, host_label())
            notes += [f"reconcile: FAILED — {e}" for e in rec["errors"]]
        closing = HeldRecords(ws).closes()  # closed by the owner while the room was unreachable
        for e in store.entries():
            if e["status"] in TERMINAL or e["ask_id"] in closing:
                done += 1
            elif not e["incomplete"]:
                rows.append(waiting_item(e["ask_id"], e["title"], e["body"], e["asked_at"], True, e["priority"]))
    except Exception as e:  # noqa: BLE001
        return _unavailable(ws, f"{type(e).__name__}: {e}", store)
    held = {r["ask_id"] for r in rows}
    rows += [it for it in outbox_items(ws) if it["ask_id"] not in held]
    return {"waiting": rows, "done": done, "unavailable": False, "reason": None, "link": store.link,
            "notes": notes, "store": store.label}


def waiting(workspace: Path) -> list:
    return gather(workspace)["waiting"]


def count(workspace: Path) -> dict:
    g = gather(workspace)
    return {"open": None if g["unavailable"] else len(g["waiting"]), "done": g["done"],
            "unavailable": g["unavailable"], "reason": g["reason"]}


def _close_locally(ws: Path, ask_id: str, status: str, why: str) -> tuple:
    try:
        p = HeldRecords(ws).close(ask_id, status, note=why)
    except Exception as e:  # noqa: BLE001
        return False, f"UNRECORDED: {why}; and the local close record failed ({type(e).__name__}: {e})"
    return True, f"recorded locally as {status} in {p} ({why}); the next reconcile applies it to the row"


def resolve(workspace: Path, ask_id: str, status: str) -> tuple:
    """(closed, message): the row's Closed cell takes `status` (Answered or Resolved) after a
    reconcile files any held question; a store that cannot be written leaves a local close
    record instead. A row the store knows and refuses (closed already, no such row) is not
    changed and nothing is recorded. Never reopens a closed row."""
    from util_paths import host_label
    ws = Path(workspace)
    store, where = room_store(ws)
    if store is None:
        return _close_locally(ws, ask_id, status, f"room database: not used ({where})")
    try:
        reconcile_pending(store, ws, host_label())
        store.close(ask_id, status)
        return True, f"room database: {ask_id} -> {status} in {store.where(ask_id)}"
    except GuardFailed as g:
        if not g.current and any(e["ask_id"] == ask_id for e in Outbox(ws).entries()):
            return _close_locally(ws, ask_id, status, "its held entry has no row yet")
        return False, f"room database: not changed — {type(g).__name__}: {g}"
    except Exception as e:  # noqa: BLE001
        return _close_locally(ws, ask_id, status, f"room database unreachable ({type(e).__name__}: {e})")


# ---- the ask ----------------------------------------------------------------------

def ask_owner(question: str, context: Optional[str] = None, urgency: str = "live",
              task_file: Optional[str] = None, workspace: Optional[Path] = None,
              host: Optional[str] = None, now: Optional[float] = None, store=None,
              default_action: Optional[str] = None, reason: Optional[str] = None,
              options=(), priority: str = "Medium") -> dict:
    """Reconcile, queue (core), hold, row, notify — each step reported, none raising past here.
    `store` is this adapter's store; None leaves the question in the outbox. No outbox record,
    no row: the question is still queued to the owner, and the report says it was not held."""
    from util_paths import host_label
    from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
    ws = Path(workspace) if workspace else resolve_workspace(migrate=False)
    host = host or host_label()
    rec = None
    if store is not None:
        try:
            rec = reconcile_pending(store, ws, host)
        except Exception as e:  # noqa: BLE001 — this ask still goes out
            rec = {"flushed": [], "closed": [], "moved": [], "errors": [f"{type(e).__name__}: {e}"]}
    introduced = status_path(ROOM_INTRODUCED, ws)
    intro = None
    if store is not None and not introduced.exists():
        intro = (f"All my questions for you are kept in the Pending questions database in {store.label}; "
                 "open it any time from that room. Questions are sent to you as they come up, not on a schedule.")
    link = getattr(store, "link", None) if store is not None else None
    out = core_ask.queue_question(question, context, task_file, ws, host, now, link, intro,
                                  default_action, reason, options, priority)
    out.update({"reconcile": rec, "record": None, "db_error": out["outbox_error"], "link": link,
                "macos": None, "macos_fix": None})
    if out["proactive_file"] and intro:
        try:
            mark_room_used(ws, out["ask_id"])
        except OSError as e:
            out["send_error"] = f"{type(e).__name__}: {e}"
    if out["outbox"] is None:
        out["db_error"] = f"{out['outbox_error']}; the room was NOT written (no durable record to replay)"
    else:
        q = Question.from_dict(out["question"])
        w = write_question(q, store, out["sent_line"])
        out["link"], out["db_error"] = w.link or link, w.db_error
        if w.complete:
            out["record"] = store.where(q.ask_id)
            Outbox(ws).delete(q.ask_id)
            out["outbox"] = None
        else:
            out["record"] = f"outbox {out['outbox']}"
    if urgency == "live":
        out["macos"], out["macos_fix"] = core_ask.notify_macos(f"Question: {question}")
    return out


def report_lines(out: dict) -> list:
    lines = core_ask.report_lines(out)
    rec = out.get("reconcile") or {}
    for err in rec.get("errors") or []:
        lines.append(f"reconcile: FAILED — {err}")
    if rec.get("flushed"):
        lines.append(f"reconcile: filed {len(rec['flushed'])} held question(s) from the outbox")
    if rec.get("closed"):
        lines.append(f"reconcile: applied {len(rec['closed'])} local close(s)")
    if rec.get("moved"):
        lines.append(f"reconcile: moved {len(rec['moved'])} legacy file entr(ies) into the database")
    return lines


def remind(argv: list, workspace: Path) -> int:
    """The reminder (pending_questions_remind.main) with this adapter injected."""
    import pending_questions_remind as reminder  # noqa: PLC0415
    return reminder.main(argv, Path(workspace))


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
    if op not in ("rows", "row"):  # a read creates nothing, not even the database
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
    if op == "guarded":
        # A precondition on this replica's view, not a distributed lock (merge order cannot
        # matter; see pending_questions_store). A missing row is "not written, nothing current".
        have = _cells(maps, db, row) if exists else {}
        current = {p: have.get(p) for p in req["expect"]}
        if not exists or any(current[p] not in allowed for p, allowed in req["expect"].items()):
            return {"written": False, "current": current}
        await doc.put_database({"cells": _cell_writes(db, row, req["cells"], by, now_ms)})
        return {"written": True, "current": current}
    if not exists:
        raise LookupError(f"no row {row!r} in database {db!r}")
    if op == "set_cells":
        await doc.put_database({"cells": _cell_writes(db, row, req["cells"], by, now_ms)})
        return None
    if op == "set_body":
        await doc.put_row_body(db, row, req["body"])
        return None
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
