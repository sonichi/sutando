#!/usr/bin/env python3
"""The room-database adapter for owner pending questions — the single reader and writer
core reaches through the manifest's `pending_questions_store` declaration.

`room_store(workspace)` is the discovery, kept at this edge: the room capability (the
`room-commons` skill, else its `room-collab` alias, in the workspace's skills then the
repo's) and the owner's DM room and agent identity from the gateway's own reading
(`state/owner-routing.json`, an `AG2SPACE_USER_ID` / `AG2_MATRIX_USER_ID` identity
winning). It returns a pending_questions_store.RoomDbStore whose client runs this
file's `serve`, or None and the reason when anything is missing.

`gather(workspace)` is READ-ONLY — it writes no file, not even the history marker — and
lists each ask id in exactly one bucket, by precedence: a terminal or locally closed row is
done; a complete open row waits (in the room); an open row with no body is stood in for by
its held entry; a held entry with no row waits (not yet in the room), or is done when closed
locally; a local close naming neither is `pending_close`. `unavailable: True` and `done: None`
when the room cannot be read — never a zero. `reconcile_pass(workspace)` is the explicit pass
(outbox replay, local closes, stale marks, the transitional legacy ingest, the history
backfill); `ask_owner` runs it first, then the queue, then the row, and writes no row when
the outbox could not hold the question; a confirmed row commits the history marker BEFORE
its outbox entry is deleted, and keeps the entry when that commit fails. `resolve` closes
the row (committing the history marker first when the row is in view and the marker is not;
a marker that cannot be written leaves the row open and records the close locally), else
records the closure locally for the next reconcile — only for a question held in the outbox,
or in outage mode once a row of this workspace was ever confirmed; an unknown id with neither
is refused and changes no count. `remind` is the reminder.

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
from pending_questions_outbox import (Outbox as HeldRecords, local_closes, mark_room_introduced,
                                      mark_store_used, room_introduced, store_history)
from pending_questions_store import (DB_SCHEMA, INCOMPLETE, TERMINAL, GuardFailed, Outbox, Question,
                                     RoomDbStore, ScriptDbClient, StoreError, outbox_items, reconcile_pending,
                                     safe_body, waiting_item, write_question)
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
# The collab service URL; set, it overrides the capability's own resolution (an outage rehearsal).
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


def manifest_config(key: str) -> str:
    """This skill's manifest `config[key]`, stripped; empty when unset or unreadable."""
    try:
        cfg = json.loads((HERE.parent / "manifest.json").read_text(encoding="utf-8"))
        return str((cfg.get("config") or {}).get(key) or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def configured(key: str, environ) -> str:
    """One config value by the documented precedence (skills/MANIFEST.md): env, then this
    skill's manifest `config`; the CLI tier is the caller's own argument."""
    return (environ.get(key) or "").strip() or manifest_config(key)


def configured_room(workspace: Path, environ) -> str:
    """The room holding the database: env, then this skill's manifest config, then
    this host's state/pending-questions-room; empty means the owner DM."""
    room = configured(ROOM_KEY, environ)
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


def room_store(workspace: Path, environ=None, timeout: float = 90.0, collab_url: Optional[str] = None):
    """(RoomDbStore, where) when the capability, the room and an identity all
    resolve; (None, why not) otherwise. The room is the configured one, else the owner DM.
    `collab_url` is the CLI tier of PENDING_QUESTIONS_COLLAB_URL (then env, then manifest)."""
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
    url_override = (collab_url or "").strip() or configured(URL_KEY, env)
    argv = [sys.executable, str(Path(__file__).resolve()), "serve", "--room", room,
            "--user-id", user, "--skill-scripts", str(scripts)]
    if url_override:
        argv += ["--collab-url", url_override]
    return RoomDbStore(ScriptDbClient(argv, timeout), label=f"the owner's {'room' if shared else 'DM room'} {room}",
                       lock=status_path("pending-questions-db.lock", Path(workspace)), host=host_label(),
                       link=_db_link(scripts, room, url_override)), room


# ---- the reader -------------------------------------------------------------------

def _unavailable(ws: Path, reason: str, store) -> dict:
    return {"waiting": outbox_items(ws), "done": None, "pending_close": local_closes(ws)[1], "unavailable": True,
            "reason": reason, "link": getattr(store, "link", None), "store": getattr(store, "label", None),
            "notes": [f"ROOM DATABASE UNAVAILABLE ({reason}); the count is unknown"]}


def _pending_note(ids: list) -> list:
    return [f"{len(ids)} local close(s) await the row they name: {', '.join(ids)}"] if ids else []


def _held_snapshot(ws: Path) -> tuple:
    """(every held ask id, {ask id: waiting item} of the held questions still open) — one read."""
    return {e["ask_id"] for e in HeldRecords(ws).entries()}, {it["ask_id"]: it for it in outbox_items(ws)}


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


def gather(workspace: Path, environ=None) -> dict:
    """{"waiting", "done", "pending_close", "unavailable", "reason", "link", "notes", "store"}, each
    ask id in ONE bucket (the precedence in the module doc): this host's rows first, then the
    outbox's held questions whose id no row already placed (marked not yet in the room);
    `pending_close` names local closes with no row and no held entry. The outbox is read before
    the rows and again after, and the two reads are joined: a held question another process
    files between them is in one of the reads, never in neither. READ-ONLY, as the contract
    says: a read writes nothing, so a row seen here is history only once `reconcile_pass`
    records it. Without a store: the outbox, and why — an outage (`unavailable`) when a row of
    this workspace was ever confirmed, a measurement when none was."""
    ws = Path(workspace)
    store, where = room_store(ws, environ)
    if store is None:
        if store_history(ws):
            return _unavailable(ws, f"room database not reachable ({where}), though it was used before", None)
        done, pending = local_closes(ws)
        return {"waiting": outbox_items(ws), "done": len(done), "pending_close": pending, "unavailable": False,
                "reason": None, "link": None, "store": None,
                "notes": [f"room database: not used ({where}); listing the local outbox only"] + _pending_note(pending)}
    notes, rows, done, placed = [], [], 0, set()
    try:
        closing = HeldRecords(ws).closes()  # closed by the owner while the room was unreachable
        in_outbox, held = _held_snapshot(ws)
        seen = set()
        for e in store.entries():
            seen.add(e["ask_id"])
            if e["status"] in TERMINAL or e["ask_id"] in closing:
                done += 1
                placed.add(e["ask_id"])
            elif not e["incomplete"]:
                rows.append(waiting_item(e["ask_id"], e["title"], e["body"], e["asked_at"], True, e["priority"]))
                placed.add(e["ask_id"])
            # an open row with no body yet: its held entry (below) stands in for it
    except Exception as e:  # noqa: BLE001
        return _unavailable(ws, f"{type(e).__name__}: {e}", store)
    after_ids, after = _held_snapshot(ws)
    in_outbox |= after_ids
    held.update(after)
    rows += [held[a] for a in sorted(held) if a not in placed]
    done += sum(1 for a in closing if a not in seen and a in in_outbox)  # held, closed locally, no row yet
    pending = sorted(a for a in closing if a not in seen and a not in in_outbox)
    return {"waiting": rows, "done": done, "pending_close": pending, "unavailable": False, "reason": None,
            "link": store.link, "notes": notes + _pending_note(pending), "store": store.label}


def waiting(workspace: Path) -> list:
    return gather(workspace)["waiting"]


def count(workspace: Path) -> dict:
    g = gather(workspace)
    return {"open": None if g["unavailable"] else len(g["waiting"]), "done": g["done"],
            "pending_close": len(g["pending_close"]), "unavailable": g["unavailable"], "reason": g["reason"]}


def local_close_allowed(ws: Path, ask_id: str) -> tuple:
    """(allowed, why): a local close stands in for the row only when the outbox holds the ask
    (the row is not born yet) or a row of this workspace was ever confirmed (outage mode)."""
    if any(e["ask_id"] == ask_id for e in HeldRecords(ws).entries()):
        return True, "held in the outbox"
    if store_history(ws):
        return True, "outage mode: a room row was confirmed here before"
    return False, f"no held question {ask_id} and no room row was ever confirmed here; nothing to close"


def _close_locally(ws: Path, ask_id: str, status: str, why: str, basis: Optional[str] = None) -> tuple:
    """`basis` given: the caller has the row in view, so the gate is not asked."""
    if basis is None:
        allowed, basis = local_close_allowed(ws, ask_id)
        if not allowed:
            return False, f"not recorded: {basis} ({why})"
    try:
        p = HeldRecords(ws).close(ask_id, status, note=why)
    except Exception as e:  # noqa: BLE001
        return False, f"UNRECORDED: {why}; and the local close record failed ({type(e).__name__}: {e})"
    return True, f"recorded locally as {status} in {p} ({why}; {basis}); the next reconcile applies it to the row"


def resolve(workspace: Path, ask_id: str, status: str) -> tuple:
    """(closed, message): the row's Closed cell takes `status` (Answered or Resolved) after a
    reconcile files any held question; a store that cannot be written leaves a local close
    record instead. A row in view before the history marker was ever committed commits it
    first; when that fails the row is left open and the close is recorded locally — the
    evidence outlives the capability either way. A row the store knows and refuses (closed
    already, no such row) is not changed and nothing is recorded. Never reopens a closed row."""
    from util_paths import host_label
    ws = Path(workspace)
    store, where = room_store(ws)
    if store is None:
        return _close_locally(ws, ask_id, status, f"room database: not used ({where})")
    try:
        reconcile_pending(store, ws, host_label())
        if not store_history(ws) and store.status_of(ask_id) is not None:
            try:
                Outbox(ws).commit_history(ask_id)
            except StoreError as e:
                return _close_locally(ws, ask_id, status, f"room database: left open — {e}", "its row is in view")
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
    no row: the question is still queued to the owner, and the report says it was not held. A
    confirmed row commits the store-history marker, then releases its outbox entry; a marker
    that cannot be written keeps the entry (`history_error` says so). The queued introduction
    marks only itself."""
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
    intro = None
    if store is not None and not room_introduced(ws):
        intro = (f"All my questions for you are kept in the Pending questions database in {store.label}; "
                 "open it any time from that room. Questions are sent to you as they come up, not on a schedule.")
    link = getattr(store, "link", None) if store is not None else None
    out = core_ask.queue_question(question, context, task_file, ws, host, now, link, intro,
                                  default_action, reason, options, priority)
    out.update({"reconcile": rec, "record": None, "db_error": out["outbox_error"], "link": link,
                "macos": None, "macos_fix": None, "history_error": None})
    if out["proactive_file"] and intro:
        try:
            mark_room_introduced(ws, out["ask_id"])
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
            try:
                mark_store_used(ws, q.ask_id)
                Outbox(ws).delete(q.ask_id)
                out["outbox"] = None
            except OSError as e:  # the entry stays: local evidence a later outage can be read from
                out["history_error"] = f"{type(e).__name__}: {e}"
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
    if out.get("history_error"):
        lines.append(f"history: FAILED — {out['history_error']}; the row landed, and its outbox entry is kept "
                     "until a reconcile commits the history")
    return lines


def remind(argv: list, workspace: Path, resolved=None) -> int:
    """The reminder (pending_questions_remind.main) on the caller's `resolved` (the core reader's
    one Resolved of this adapter, carried through, nothing resolved again); without one, this
    adapter injected by its own file, so the reader's one load serves it (never a re-import)."""
    import pending_questions_remind as reminder  # noqa: PLC0415
    argv = list(argv)
    if resolved is None and "--store-adapter" not in argv:
        argv += ["--store-adapter", __file__]
    return reminder.main(argv, Path(workspace), adapter=resolved)


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
