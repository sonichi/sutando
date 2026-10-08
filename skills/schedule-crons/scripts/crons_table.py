#!/usr/bin/env python3
"""The Crons room database: one row per crons.json entry per host, in the owner's room, so anyone
can see what every agent has scheduled.

  sync                  upsert this host's rows from <workspace>/hosts/<host>/crons.json; an entry
                        gone from it is marked Finished (its row is kept). Skipped when the rows it
                        would write match the last synced digest; --force writes anyway, which is
                        also how rows deleted on the room side are restored.
  touch NAME RESULT     stamp "Last ran (UTC)" and "Last result" on this host's row.
  status NAME STATUS    set Status (Active, Paused or Finished) on this host's row.

Rows are keyed per host and cron name, so two hosts never write each other's rows. sync owns the
columns crons.json defines on the rows it created; "Last ran (UTC)" and "Last result" are written
only by `touch`. Status is set by sync only on a new row, an entry gone from crons.json (Finished),
a Finished row whose entry is back, an empty cell, or an entry whose crons.json status (Paused when
`disabled: true` or DISABLED in the name) changed since the last sync; otherwise a Status set by
`status` stays.

A database already named "Crons" in the room is adopted in place: matching columns are reused,
missing ones are added. A row sync did not create (adopted by its title) only has its empty cells
filled from crons.json; a value someone typed there is never overwritten, except Status when that
entry's crons.json status changes (disabling or re-enabling it is an explicit change of schedule).

The room is CRONS_TABLE_ROOM (--room, env, then this skill's manifest), else the owner's DM room.
Fail-open: without the room capability, a room, an identity or a connection, one line is printed
and the exit status is 2; nothing raises into the caller. A crons.json that cannot be read exits 1.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
HERE = Path(__file__).resolve().parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
from cron_ownership import entry_owner  # noqa: E402
from owner_room_access import (agent_identity, capability_scripts, owner_dm, owner_routing,  # noqa: E402
                               resolve_credentials)

# (skill directory, CLI module, client module), canonical layout first.
CAPABILITY_LAYOUTS = (("room-commons", "room_commons", "room_commons_client"),
                      ("room-collab", "room_collab", "room_collab_client"))
# The client's open function: the current name first; releases before the rename export only the second.
OPENERS = ("open_room_commons", "open_room_collab")
TIMEOUT_SEC = 120.0
ROOM_KEY = "CRONS_TABLE_ROOM"
DB_KEY = "CRONS_TABLE_DB"
DEFAULT_DB = "Crons"
NEW_DB_ID = "crons"  # fixed, so two hosts creating the database at once write the same keys
STAMP = "crons-table-stamp.json"
SETTLE_SEC = 1.0
GAP = 1024
WHAT_MAX = 140
STATUSES = ("Active", "Paused", "Finished")
RUNNERS = ("session", "launchd", "codex-task", "monitor", "dynamic-loop")
EXIT_CONFIG, EXIT_UNAVAILABLE = 1, 2


def _opts(names, colors):
    return [{"id": f"o{i}", "name": n, "color": c} for i, (n, c) in enumerate(zip(names, colors))]


# (column id, name, type, options); names are the shared contract, ids only name new columns.
COLUMNS = (
    ("cron", "Cron", "title", None),
    ("host", "Host", "text", None),
    ("schedule", "Schedule", "text", None),
    ("timezone", "Timezone", "text", None),
    ("what", "What", "text", None),
    ("defined_in", "Defined in", "text", None),
    ("owner", "Owner", "text", None),
    ("runner", "Runner", "select", _opts(RUNNERS, ("blue", "purple", "orange", "pink", "brown"))),
    ("status", "Status", "select", _opts(STATUSES, ("green", "yellow", "gray"))),
    ("last_ran", "Last ran (UTC)", "text", None),
    ("last_result", "Last result", "text", None),
)
NAMES = {c[0]: c[1] for c in COLUMNS}
STAMPED = ("last_ran", "last_result")
SELECT_TYPES = ("select", "status", "multi_select")


# ---- rows from crons.json (pure) ---------------------------------------------------

def runner_of(entry: dict) -> str:
    if isinstance(entry.get("monitor"), dict):
        return "monitor"
    if entry.get("loop") == "dynamic":
        return "dynamic-loop"
    if entry.get("execution") == "codex-task":
        return "codex-task"
    if entry.get("launchd") is True:
        return "launchd"
    return "session"


def status_of(entry: dict) -> str:
    name = entry.get("name")
    paused = entry.get("disabled") is True or (isinstance(name, str) and "DISABLED" in name)
    return "Paused" if paused else "Active"


def schedule_of(entry: dict) -> str:
    if isinstance(entry.get("cron"), str) and entry["cron"].strip():
        return entry["cron"].strip()
    return {"dynamic-loop": "dynamic", "monitor": "continuous"}.get(runner_of(entry), "")


def _one_line(text: str, limit: int = WHAT_MAX) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def what_of(entry: dict) -> str:
    if isinstance(entry.get("prompt_skill"), str) and entry["prompt_skill"].strip():
        return "/" + entry["prompt_skill"].strip().lstrip("/")
    if runner_of(entry) == "monitor":
        mon = entry["monitor"]
        return _one_line(mon.get("description") or mon.get("command") or "")
    return _one_line(entry.get("prompt") or "")


def codex_default_timezone() -> Optional[str]:
    """The Codex runner's own default zone, read from it; None when it cannot be loaded."""
    try:
        spec = importlib.util.spec_from_file_location("_codex_scheduler", HERE / "codex-scheduler.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return str(mod.DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001 — a display column, never a precondition
        return None


def local_timezone() -> str:
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return time.strftime("%Z")


def timezone_of(entry: dict, local_tz: str, codex_tz: Optional[str]) -> str:
    if isinstance(entry.get("timezone"), str) and entry["timezone"].strip():
        return entry["timezone"].strip()
    if runner_of(entry) == "codex-task" and codex_tz:
        return codex_tz
    return local_tz


def build_rows(entries, host: str, defined_in: str, local_tz: str, codex_tz: Optional[str] = None) -> dict:
    """{cron name: {column id: value}} for every named entry; a repeated name keeps the last."""
    rows = {}
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"].strip():
            continue
        name = e["name"].strip()
        rows[name] = {"cron": name, "host": host, "schedule": schedule_of(e),
                      "timezone": timezone_of(e, local_tz, codex_tz), "what": what_of(e),
                      "defined_in": defined_in, "owner": entry_owner(e), "runner": runner_of(e),
                      "status": status_of(e)}
    return rows


def rows_digest(rows: dict, room: str, db_name: str) -> str:
    blob = json.dumps([room, db_name, rows], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def utc_minute(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() if now is None else now))


# ---- planning writes over the database maps (pure; `rd` is the capability's room_database) --

def _fold(s) -> str:
    return " ".join(str(s).split()).casefold()


def _merged(maps: dict, writes: dict) -> dict:
    out = {m: dict(maps.get(m) or {}) for m in ("dbs", "props", "rows", "cells", "views")}
    for m, entries in writes.items():
        for k, v in entries.items():
            if v is None:
                out[m].pop(k, None)
            else:
                out[m][k] = v
    return out


def _put(writes: dict, m: str, k: str, v) -> None:
    writes.setdefault(m, {})[k] = v


class Plan:
    """The writes for one command on one database, in the shapes DATABASE.md defines."""

    def __init__(self, rd, maps: dict, db_name: str, by: str, now_ms: int):
        self.rd, self.by, self.now_ms = rd, by, now_ms
        self.writes: dict = {}
        self.notes: list = []
        self.created: list = []
        self.finished: list = []
        hit = [d for d in rd.list_dbs(maps) if _fold(d["name"]) == _fold(db_name)]
        if len(hit) > 1:
            raise ValueError(f"{len(hit)} databases are named {db_name!r} in this room; leave one")
        if hit:
            self.db = hit[0]["id"]
        else:
            self.db = NEW_DB_ID
            if NEW_DB_ID in (maps.get("dbs") or {}):
                raise ValueError(f"database id {NEW_DB_ID!r} is taken by another name; set {DB_KEY}")
            _put(self.writes, "dbs", self.db, {"name": db_name, "order": (len(maps.get("dbs") or {}) + 1) * GAP,
                                               "created": now_ms, "by": by})
            _put(self.writes, "views", rd.key(self.db, "vtable"),
                 {"name": "All crons", "layout": "table", "order": GAP,
                  "sort": [{"prop": "host", "dir": "asc"}, {"prop": "cron", "dir": "asc"}]})
        self.maps = _merged(maps, self.writes)
        self._ensure_columns()

    def _read(self) -> dict:
        return self.rd.read_db(self.maps, self.db)

    def _ensure_columns(self) -> None:
        d = self._read()
        last = max((p["order"] for p in d["props"]), default=0)
        self.prop = {}
        for cid, name, typ, options in COLUMNS:
            have = [p for p in d["props"] if _fold(p["name"]) == _fold(name)]
            if have:
                self.prop[cid] = have[0]["id"]
                continue
            pid = cid if not any(p["id"] == cid for p in d["props"]) else self.rd.new_id("p")
            last += GAP
            # An adopted database keeps its own title column; ours becomes text there.
            if typ == "title" and any(p["type"] == "title" for p in d["props"]):
                typ = "text"
            entry = {"name": name, "type": typ, "order": last, **({"options": options} if options else {})}
            self._write("props", self.rd.key(self.db, pid), entry)
            self.prop[cid] = pid

    def _write(self, m: str, k: str, v) -> None:
        _put(self.writes, m, k, v)
        self.maps = _merged(self.maps, {m: {k: v}})

    def _prop(self, cid: str) -> dict:
        pid = self.prop[cid]
        return next(p for p in self._read()["props"] if p["id"] == pid)

    def _value(self, cid: str, raw):
        """The stored form of `raw` for this column, appending a missing option; UNFIT when it cannot fit."""
        prop = self._prop(cid)
        if cid == "last_ran" and prop["type"] == "date" and isinstance(raw, str):
            raw = raw.replace(" ", "T")
        v = self.rd.normalize(prop, raw)
        if v is self.rd.UNFIT and prop["type"] in SELECT_TYPES and isinstance(raw, str):
            opts = list(prop.get("options") or [])
            opts.append({"id": self.rd.new_id("o"), "name": raw, "color": "gray"})
            self._write("props", self.rd.key(self.db, prop["id"]),
                        {k: v for k, v in {**prop, "options": opts}.items() if k != "id"})
            v = self.rd.normalize(self._prop(cid), raw)
        return v

    def _cell(self, row: str, cid: str):
        c = self.maps["cells"].get(self.rd.key(self.db, row, self.prop[cid]))
        return c.get("v") if isinstance(c, dict) else None

    def _display(self, row: str, cid: str) -> str:
        v, prop = self._cell(row, cid), self._prop(cid)
        if prop["type"] in SELECT_TYPES:
            ids = v if isinstance(v, list) else [v]
            names = [next((o.get("name") for o in prop.get("options") or [] if o.get("id") == i), i) for i in ids]
            return ", ".join(str(n) for n in names if n is not None)
        return "" if v is None else str(v)

    def row_id(self, host: str, name: str) -> str:
        return "c" + hashlib.sha256(f"{host}\n{name}".encode("utf-8")).hexdigest()[:16]

    def find_row(self, host: str, name: str) -> Optional[str]:
        """This host's row for `name`: its own id, else a row titled `name` for this host, else one
        titled `name` with no host (an adopted row)."""
        rows = [r["id"] for r in self._read()["rows"]]
        mine = self.row_id(host, name)
        if mine in rows:
            return mine
        titled = [r for r in rows if _fold(self._display(r, "cron")) == _fold(name)]
        return (next((r for r in titled if self._display(r, "host") == host), None)
                or next((r for r in titled if not self._display(r, "host")), None))

    def host_rows(self, host: str) -> dict:
        return {self._display(r["id"], "cron"): r["id"] for r in self._read()["rows"]
                if self._display(r["id"], "host") == host and self._display(r["id"], "cron")}

    def set_cells(self, row: str, values: dict) -> None:
        for cid, raw in values.items():
            v = self._value(cid, raw)
            if v is self.rd.UNFIT:
                self.notes.append(f"{NAMES[cid]}: {raw!r} does not fit this database's column; left as is")
                continue
            if v == self._cell(row, cid):
                continue
            self._write("cells", self.rd.key(self.db, row, self.prop[cid]),
                        None if v is None else {"v": v, "updated": self.now_ms, "by": self.by})

    def _empty(self, row: str, cid: str) -> bool:
        return self._cell(row, cid) in (None, "", [])

    def upsert(self, host: str, name: str, derived: dict, explicit: Optional[dict] = None,
               on_create: Optional[dict] = None) -> str:
        """`derived` (from crons.json) is written on a row this host's sync created, and only fills
        empty cells on an adopted row; `explicit` is always written; `on_create` only on a new row."""
        row = self.find_row(host, name)
        base = {"cron": name, "host": host, **derived}
        if row is None:
            row = self.row_id(host, name)
            last = max((r["order"] for r in self._read()["rows"]), default=0)
            self._write("rows", self.rd.key(self.db, row), {"order": last + GAP, "created": self.now_ms, "by": self.by})
            self.created.append(name)
            base.update(on_create or {})
        elif row != self.row_id(host, name):
            base = {c: v for c, v in base.items() if self._empty(row, c)}
        self.set_cells(row, {**base, **(explicit or {})})
        return row


def _split(derived: Optional[dict]) -> tuple:
    """(the crons.json columns sync may refresh, the status a new row starts with)."""
    d = {k: v for k, v in (derived or {}).items() if k not in STAMPED}
    status = d.pop("status", None)
    return d, ({"status": status} if status else {})


def plan_sync(rd, maps: dict, db_name: str, host: str, rows: dict, by: str, now_ms: int,
              synced: Optional[dict] = None) -> Plan:
    """`synced` is {name: status} as crons.json gave it at the last sync; an entry whose crons.json
    status moved since then has it written, while a Status set by hand otherwise stays."""
    plan = Plan(rd, maps, db_name, by, now_ms)
    synced = synced if isinstance(synced, dict) else {}
    kept = set()
    for name, values in rows.items():
        derived, start = _split(values)
        row = plan.find_row(host, name)
        moved = name in synced and synced[name] != values.get("status")
        reset = row is not None and (moved or plan._display(row, "status") == "Finished"
                                     or plan._empty(row, "status"))
        kept.add(plan.upsert(host, name, derived, start if reset else None, start))
    for name, row in plan.host_rows(host).items():
        if row not in kept and plan._display(row, "status") != "Finished":
            plan.set_cells(row, {"status": "Finished"})
            plan.finished.append(name)
    return plan


def plan_touch(rd, maps: dict, db_name: str, host: str, name: str, result: str, derived: Optional[dict],
               by: str, now_ms: int) -> Plan:
    plan = Plan(rd, maps, db_name, by, now_ms)
    cols, start = _split(derived)
    plan.upsert(host, name, cols,
                {"last_ran": utc_minute(now_ms / 1000), "last_result": _one_line(result, 2000)}, start)
    return plan


def plan_status(rd, maps: dict, db_name: str, host: str, name: str, status: str, derived: Optional[dict],
                by: str, now_ms: int) -> Plan:
    plan = Plan(rd, maps, db_name, by, now_ms)
    plan.upsert(host, name, _split(derived)[0], {"status": status})
    return plan


# ---- the edge: workspace, config, capability, room --------------------------------

def manifest_config(key: str) -> str:
    try:
        cfg = json.loads((HERE.parent / "manifest.json").read_text(encoding="utf-8"))
        return str((cfg.get("config") or {}).get(key) or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def configured(key: str, cli: Optional[str], environ) -> str:
    """CLI, then env, then this skill's manifest `config` (skills/MANIFEST.md precedence)."""
    return (cli or "").strip() or (environ.get(key) or "").strip() or manifest_config(key)


def crons_file(workspace: Path, host: str) -> Path:
    return Path(workspace) / "hosts" / host / "crons.json"


def load_entries(path: Path) -> list:
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"{path} does not hold a list")
    return entries


def read_stamp(workspace: Path, host: str) -> dict:
    try:
        d = json.loads((Path(workspace) / "hosts" / host / STAMP).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_stamp(workspace: Path, host: str, digest: str, room: str, rows: dict) -> None:
    p = Path(workspace) / "hosts" / host / STAMP
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}")
    statuses = {n: r.get("status") for n, r in rows.items()}
    tmp.write_text(json.dumps({"ts": int(time.time()), "digest": digest, "room": room, "rows": len(rows),
                               "statuses": statuses}) + "\n", encoding="utf-8")
    os.replace(tmp, p)


class Unavailable(Exception):
    """The room cannot be reached; the message is the one line printed."""


def target(workspace: Path, room_cli: Optional[str], environ) -> tuple:
    """(capability scripts dir, room, identity); Unavailable when any is missing."""
    roots = (Path(workspace) / "skills", REPO / "skills")
    scripts = next((s for s in (capability_scripts(roots, (skill,), (f"{cli}.py", f"{client}.py"))
                                for skill, cli, client in CAPABILITY_LAYOUTS) if s), None)
    if scripts is None:
        raise Unavailable("no room capability installed (" + " or ".join(x[0] for x in CAPABILITY_LAYOUTS) + ")")
    routing = owner_routing(workspace)
    room = configured(ROOM_KEY, room_cli, environ) or owner_dm(routing)
    if not room:
        raise Unavailable(f"no room: {ROOM_KEY} is unset and state/owner-routing.json has no owner_dm")
    user = agent_identity(routing, environ)
    if not user:
        raise Unavailable("no agent identity to sign database writes with")
    return scripts, room, user


def load_capability(scripts: Path):
    """(the CLI module, its open function, room_database) from the injected capability; its client
    may re-exec this process onto an interpreter that has its dependencies, before anything is written."""
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


async def run_plan(scripts: Path, room: str, user: str, build):
    """Open the room's databases, plan with `build(rd, maps, by, now_ms)`, write it in one update."""
    cap, open_room, rd = load_capability(scripts)
    url, token = resolve_credentials(cap, None)
    async with open_room(url, room, token, kind="db") as doc:
        plan = build(rd, doc.database, user, int(time.time() * 1000))
        if plan.writes:
            await doc.put_database(plan.writes)
            await doc.settle(SETTLE_SEC)
        return plan


def _derived(workspace: Path, host: str, name: str) -> Optional[dict]:
    try:
        rows = build_rows(load_entries(crons_file(workspace, host)), host, _defined_in(host), local_timezone(),
                          codex_default_timezone())
    except (OSError, ValueError):
        return None
    return rows.get(name)


def _defined_in(host: str) -> str:
    return f"hosts/{host}/crons.json"


def main(argv=None, environ=None, runner=run_plan) -> int:
    from util_paths import host_label
    from workspace_default import resolve_workspace
    env = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(description="The Crons room database for this host's crons.json.")
    ap.add_argument("--workspace", default=None, help="workspace dir (default: the resolved workspace)")
    ap.add_argument("--host", default=None, help="host label (default: this host's)")
    ap.add_argument("--room", default=None, help=f"room id (default: {ROOM_KEY}, else the owner's DM)")
    ap.add_argument("--db", default=None, help=f"database name (default: {DB_KEY}, else {DEFAULT_DB!r})")
    ap.add_argument("--timeout", type=float, default=TIMEOUT_SEC, help="seconds for the whole room exchange")
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--force", action="store_true", help="write even when nothing changed since the last sync")
    t = sub.add_parser("touch")
    t.add_argument("name")
    t.add_argument("result")
    st = sub.add_parser("status")
    st.add_argument("name")
    st.add_argument("status", type=lambda x: x.capitalize(), choices=STATUSES)
    args = ap.parse_args(argv)

    workspace = Path(args.workspace) if args.workspace else resolve_workspace(migrate=False)
    host = args.host or host_label()
    db_name = configured(DB_KEY, args.db, env) or DEFAULT_DB
    try:
        scripts, room, user = target(workspace, args.room, env)
    except Unavailable as e:
        scripts = room = user = None
        unavailable = str(e)
    else:
        unavailable = None

    if args.command == "sync":
        path = crons_file(workspace, host)
        try:
            rows = build_rows(load_entries(path), host, _defined_in(host), local_timezone(), codex_default_timezone())
        except (OSError, ValueError) as e:
            print(f"crons-table: not synced — cannot read {path}: {type(e).__name__}: {e}")
            return EXIT_CONFIG
        if unavailable:
            print(f"crons-table: not synced — {unavailable}")
            return EXIT_UNAVAILABLE
        digest = rows_digest(rows, room, db_name)
        stamp = read_stamp(workspace, host)
        if not args.force and stamp.get("digest") == digest:
            print(f"crons-table: unchanged ({len(rows)} rows for {host}; digest {digest})")
            return 0

        def build(rd, maps, by, now_ms):
            return plan_sync(rd, maps, db_name, host, rows, by, now_ms, stamp.get("statuses"))
    else:
        if unavailable:
            print(f"crons-table: not written — {unavailable}")
            return EXIT_UNAVAILABLE
        derived = _derived(workspace, host, args.name)

        def build(rd, maps, by, now_ms):
            if args.command == "touch":
                return plan_touch(rd, maps, db_name, host, args.name, args.result, derived, by, now_ms)
            return plan_status(rd, maps, db_name, host, args.name, args.status, derived, by, now_ms)

    try:
        plan = asyncio.run(asyncio.wait_for(runner(scripts, room, user, build), args.timeout))
    except (Exception, SystemExit) as e:  # noqa: BLE001 — fail-open: one line, never a raise into the caller
        print(f"crons-table: not written — {type(e).__name__}: {e}")
        return EXIT_UNAVAILABLE
    for note in plan.notes:
        print(f"crons-table: note — {note}")
    if args.command == "sync":
        try:
            write_stamp(workspace, host, digest, room, rows)
        except OSError as e:
            print(f"crons-table: synced, but the digest stamp failed ({e}); the next sync writes again")
        print(f"crons-table: synced {len(rows)} rows for {host} to {db_name!r} in {room} "
              f"({len(plan.created)} new, {len(plan.finished)} finished)")
    else:
        print(f"crons-table: {args.command} {args.name} on {host} in {db_name!r} ({room})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
