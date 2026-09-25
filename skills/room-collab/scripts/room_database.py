"""The room's databases, as an agent reads and writes them — the web client's model, in Python.

Mirrors cinny `dbModel.ts` over plain dicts (the five maps, see DATABASE.md): typed
values that are refused rather than written, views as queries over the same rows,
and the built-in templates. Writes are returned as plans, {map: {key: value|None}},
which the client applies in one commit; nothing here does I/O.
"""
from __future__ import annotations

import csv
import functools
import io
import math
import re
import secrets
import time
import unicodedata

from room_collab_protocol import RoomDocError

DB_KIND = "db"
MAPS = ("dbs", "props", "rows", "cells", "views")
GAP = 1024
PROP_TYPES = ("title", "text", "number", "checkbox", "url", "email", "select", "multi_select",
              "status", "date", "person", "files", "relation", "created_time", "created_by",
              "edited_time", "edited_by")
LAYOUTS = ("table", "board", "calendar", "list", "gallery")
COLORS = ("gray", "brown", "orange", "yellow", "green", "blue", "purple", "pink", "red")
AUTO = frozenset({"created_time", "created_by", "edited_time", "edited_by"})
FILTER_OPS = ("is", "is_not", "contains", "empty", "not_empty", "gt", "lt", "checked", "unchecked")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")
MXID_RE = re.compile(r"^@[^\s:]+:\S+$")
URL_RE = re.compile(r"^https?://\S+$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# The strings JavaScript's Number() reads as a number (after trimming whitespace).
JS_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
JS_RADIX_RE = re.compile(r"^0([xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)$")


class Unfit:
    """normalize()'s answer for a value that does not fit its property: never written."""

    def __repr__(self) -> str:
        return "UNFIT"


UNFIT = Unfit()


# A filter with no value: JavaScript's undefined, which equals nothing (null is None).
_MISSING = Unfit()


class DbRefusal(RoomDocError):
    """A value, name or move the model refuses; nothing was written."""


def new_id(prefix: str) -> str:
    ms, digits = int(time.time() * 1000), "0123456789abcdefghijklmnopqrstuvwxyz"
    b36 = ""
    while ms:
        ms, r = divmod(ms, 36)
        b36 = digits[r] + b36
    return prefix + b36 + "".join(secrets.choice(digits) for _ in range(5))


def key(*parts: str) -> str:
    return "|".join(parts)


def js_number(v) -> float:
    """JavaScript's Number(v) for the inputs a filter or a cell can carry."""
    if v is _MISSING:
        return math.nan
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0.0
        if JS_NUM_RE.match(s):
            return float(s)
        if JS_RADIX_RE.match(s):
            return float(int(s, 0))
        if s in ("Infinity", "+Infinity", "-Infinity"):
            return float(s.replace("Infinity", "inf"))
    return math.nan


def js_str(v) -> str:
    """JavaScript's String(v): 12.0 is "12", True is "true"."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e21:
            return str(int(v))
        return repr(v)
    return str(v)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _js_eq(a, b) -> bool:
    """JavaScript's ===: no bool/number crossover, and objects are never equal by value."""
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return a == b
    return type(a) is type(b) and a == b


def normalize(prop: dict, v):
    """A value as the property's type allows it; None to remove the cell; UNFIT when it does not fit."""
    if v is None or v == "" or (isinstance(v, list) and not v):
        return None
    options = prop.get("options") or []

    def opt(x):
        return next((o["id"] for o in options if _js_eq(o.get("id"), x) or _js_eq(o.get("name"), x)), None)

    t = prop.get("type")
    if t in ("title", "text"):
        return v[:20_000] if isinstance(v, str) else UNFIT
    if t == "url":
        return v if isinstance(v, str) and URL_RE.match(v) else UNFIT
    if t == "email":
        return v if isinstance(v, str) and EMAIL_RE.match(v) else UNFIT
    if t == "number":
        n = float(v) if _is_number(v) else js_number(v) if isinstance(v, str) else math.nan
        if not math.isfinite(n):
            return UNFIT
        return int(n) if n.is_integer() and abs(n) < 2 ** 53 else n
    if t == "checkbox":
        if isinstance(v, bool):
            return v
        return True if v == "true" else False if v == "false" else UNFIT
    if t in ("select", "status"):
        found = opt(v)
        return found if found is not None else UNFIT
    if t == "multi_select":
        ids = [opt(x) for x in (v if isinstance(v, list) else [v])]
        return list(dict.fromkeys(ids)) if all(ids) else UNFIT
    if t == "date":
        d = {"start": v} if isinstance(v, str) else v
        ok = (isinstance(d, dict) and isinstance(d.get("start"), str) and DATE_RE.match(d["start"])
              and ("end" not in d or (isinstance(d["end"], str) and DATE_RE.match(d["end"]))))
        return d if ok else UNFIT
    if t == "person":
        ids = v if isinstance(v, list) else [v]
        return (list(dict.fromkeys(ids)) if all(isinstance(x, str) and MXID_RE.match(x) for x in ids)
                else UNFIT)
    if t == "relation":
        ids = v if isinstance(v, list) else [v]
        return list(dict.fromkeys(ids)) if all(isinstance(x, str) and len(x) < 80 for x in ids) else UNFIT
    if t == "files":
        return (v if isinstance(v, list) and all(isinstance(f, dict) and isinstance(f.get("name"), str)
                                                  and isinstance(f.get("url"), str) for f in v)
                else UNFIT)
    return UNFIT  # automatic properties are computed, never written


# ---- reading ------------------------------------------------------------------

def _ordered(xs: list[dict]) -> list[dict]:
    return sorted(xs, key=lambda x: (x["order"], x["id"]))


def _num(v) -> bool:
    return _is_number(v)


def read_db(maps: dict, db: str) -> dict:
    """Everything about one database: {id, props, rows, views, cells} (props/rows/views ordered)."""
    pre = f"{db}|"

    def pick(name: str, keep) -> list[dict]:
        out = []
        for k, v in (maps.get(name) or {}).items():
            if k.startswith(pre) and isinstance(v, dict) and _num(v.get("order")):
                ident = k[len(pre):]
                if keep(ident, v):
                    out.append({**v, "id": ident})
        return _ordered(out)

    props = pick("props", lambda _i, v: v.get("type") in PROP_TYPES)
    rows = pick("rows", lambda i, _v: "|" not in i)
    views = pick("views", lambda _i, v: v.get("layout") in LAYOUTS)
    cells = {k[len(pre):]: v for k, v in (maps.get("cells") or {}).items()
             if k.startswith(pre) and isinstance(v, dict)}
    return {"id": db, "props": props, "rows": rows, "views": views, "cells": cells}


def cell(d: dict, row: str, prop: str) -> dict | None:
    return d["cells"].get(key(row, prop))


def _iso_minute(ms) -> str:
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime((ms or 0) / 1000))


def value_of(d: dict, row: dict, prop: dict):
    """A row's value for a property, including the automatic ones."""
    t = prop.get("type")
    if t == "created_time":
        return _iso_minute(row.get("created"))
    if t == "created_by":
        return [row.get("by")]
    if t in ("edited_time", "edited_by"):
        touched = [c for c in (cell(d, row["id"], p["id"]) for p in d["props"]) if c and c.get("updated")]
        last = max(touched, key=lambda c: c["updated"], default=None)
        if t == "edited_time":
            return _iso_minute(last["updated"] if last else row.get("created"))
        return [last.get("by") if last else row.get("by")]
    c = cell(d, row["id"], prop["id"])
    return c.get("v") if c else None


def display_value(v, prop: dict) -> str:
    options = prop.get("options") or []
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(
            next((o["name"] for o in options if o.get("id") == x), x) if isinstance(x, str)
            else (x or {}).get("name", "") for x in v)
    if isinstance(v, dict):
        return f"{v.get('start')} → {v['end']}" if v.get("end") else js_str(v.get("start"))
    if prop.get("type") in ("select", "status"):
        return next((o["name"] for o in options if o.get("id") == v), "")
    return js_str(v)


def _locale_key(s: str):
    # localeCompare's order, near enough: punctuation < digits < letters, then accents, then lower before upper.
    folded = s.casefold()
    base = "".join(c for c in unicodedata.normalize("NFD", folded) if not unicodedata.combining(c))
    return ([(0 if not c.isalnum() else 1 if c.isdigit() else 2, c) for c in base],
            unicodedata.normalize("NFD", folded), s.swapcase())


def _cmp_text(a: str, b: str) -> int:
    ka, kb = _locale_key(a), _locale_key(b)
    return (ka > kb) - (ka < kb)


def _passes(d: dict, row: dict, f: dict, by_id: dict) -> bool:
    p = by_id.get(f.get("prop"))
    if not p:
        return True
    v = value_of(d, row, p)
    s = display_value(v, p).lower()
    fv = f.get("value", _MISSING)
    want = fv.lower() if isinstance(fv, str) else fv
    op = f.get("op")
    if op == "empty":
        return v is None
    if op == "not_empty":
        return v is not None
    if op == "checked":
        return v is True
    if op == "unchecked":
        return v is not True
    if op == "contains":
        return isinstance(want, str) and want in s
    if op in ("is", "is_not"):
        hit = (any(_js_eq(x, fv) for x in v) if isinstance(v, list)
               else _js_eq(v, fv) or (isinstance(want, str) and s == want))
        return hit if op == "is" else not hit
    if op in ("gt", "lt"):
        if _is_number(v):
            n = js_number(fv)
            return (v > n) if op == "gt" else (v < n)
        w = "undefined" if fv is _MISSING else js_str(want)
        return (s > w) if op == "gt" else (s < w)
    return True


def query(d: dict, view: dict) -> list[dict]:
    """The rows a view shows, in its order, after its filters."""
    by_id = {p["id"]: p for p in d["props"]}
    rows = [r for r in d["rows"] if all(_passes(d, r, f, by_id) for f in (view.get("filter") or []))]
    sorts = view.get("sort") or []

    def compare(a: dict, b: dict) -> int:
        for s in sorts:
            p = by_id.get(s.get("prop"))
            if p:
                va, vb = value_of(d, a, p), value_of(d, b, p)
                # Empty sorts last in either direction, as in the web client.
                if va is None or vb is None:
                    if (va is None) != (vb is None):
                        return 1 if va is None else -1
                    continue
                c = ((va > vb) - (va < vb) if _is_number(va) and _is_number(vb)
                     else _cmp_text(display_value(va, p), display_value(vb, p)))
                if c:
                    return -c if s.get("dir") == "desc" else c
        return (a["order"] > b["order"]) - (a["order"] < b["order"]) or \
            (a["id"] > b["id"]) - (a["id"] < b["id"])

    return sorted(rows, key=functools.cmp_to_key(compare))


def groups(d: dict, view: dict, rows: list[dict]) -> list[dict]:
    """Board columns: one per option of the group property (in option order), plus "No value"."""
    p = next((x for x in d["props"] if x["id"] == view.get("groupBy")), None)
    if not p:
        return [{"id": None, "name": "All", "rows": rows}]
    none = {"id": None, "name": f"No {p['name']}", "rows": []}
    if p["type"] == "person":
        people: dict[str, list] = {}
        for r in rows:
            for m in value_of(d, r, p) or [""]:
                people.setdefault(m, []).append(r)
        return [{"id": m or None, "name": m or none["name"], "rows": rs} for m, rs in people.items()]
    cols = [{"id": o["id"], "name": o["name"], "color": o.get("color"), "rows": []}
            for o in (p.get("options") or [])]
    for r in rows:
        v = value_of(d, r, p)
        ids = v if isinstance(v, list) else [] if v is None else [v]
        hit = [c for c in cols if c["id"] in ids]
        for c in hit or [none]:
            c["rows"].append(r)
    return cols + [none] if none["rows"] else cols


def list_dbs(maps: dict) -> list[dict]:
    out = []
    for ident, v in (maps.get("dbs") or {}).items():
        if isinstance(v, dict) and isinstance(v.get("name"), str) and _num(v.get("order")):
            out.append({"id": ident, "name": v["name"], "order": v["order"],
                        **({"template": v["template"]} if isinstance(v.get("template"), str) else {})})
    return _ordered(out)


# ---- templates (identical to dbModel.ts TEMPLATES) ----------------------------

def _opts(names: list[str], colors: list[str], phases: list[str] | None = None) -> list[dict]:
    return [{"id": f"o{i}", "name": n, "color": colors[i % len(colors)],
             **({"group": phases[i]} if phases else {})} for i, n in enumerate(names)]


TEMPLATES: dict[str, dict] = {
    "tasks": {
        "name": "Tasks",
        "props": [
            {"id": "name", "name": "Name", "type": "title"},
            {"id": "status", "name": "Status", "type": "status",
             "options": _opts(["Not started", "In progress", "Done"], ["gray", "blue", "green"],
                              ["todo", "doing", "done"])},
            {"id": "assignee", "name": "Assignee", "type": "person"},
            {"id": "due", "name": "Due", "type": "date"},
            {"id": "priority", "name": "Priority", "type": "select",
             "options": _opts(["High", "Medium", "Low"], ["red", "yellow", "gray"])},
        ],
        "views": [
            {"name": "Board", "layout": "board", "groupBy": "status"},
            {"name": "Table", "layout": "table"},
            {"name": "Calendar", "layout": "calendar", "dateProp": "due"},
        ],
    },
    "meetings": {
        "name": "Meeting notes",
        "props": [
            {"id": "name", "name": "Meeting", "type": "title"},
            {"id": "date", "name": "Date", "type": "date"},
            {"id": "attendees", "name": "Attendees", "type": "person"},
            {"id": "type", "name": "Type", "type": "select",
             "options": _opts(["Standup", "Planning", "Review", "1:1"], ["blue", "purple", "orange", "pink"])},
        ],
        "views": [
            {"name": "List", "layout": "list", "sort": [{"prop": "date", "dir": "desc"}]},
            {"name": "Calendar", "layout": "calendar", "dateProp": "date"},
        ],
    },
    "demo_day": {
        "name": "Demo day",
        "props": [
            {"id": "name", "name": "Use case", "type": "title"},
            {"id": "presenter", "name": "Presenter", "type": "text"},
            {"id": "date", "name": "Demo date", "type": "date"},
            {"id": "minutes", "name": "Minutes", "type": "number"},
            {"id": "video", "name": "Demo video", "type": "url"},
            {"id": "status", "name": "Killer use case", "type": "status",
             "options": _opts(["Proposed", "Tried by others", "Confirmed"], ["gray", "yellow", "green"],
                              ["todo", "doing", "done"])},
        ],
        "views": [
            {"name": "By date", "layout": "table", "sort": [{"prop": "date", "dir": "asc"}]},
            {"name": "By status", "layout": "board", "groupBy": "status"},
            {"name": "Calendar", "layout": "calendar", "dateProp": "date"},
        ],
    },
    "wiki": {
        "name": "Docs",
        "props": [
            {"id": "name", "name": "Title", "type": "title"},
            {"id": "tags", "name": "Tags", "type": "multi_select",
             "options": _opts(["Guide", "Reference", "Decision", "How-to"], ["blue", "green", "purple", "orange"])},
            {"id": "owner", "name": "Owner", "type": "person"},
        ],
        "views": [
            {"name": "Gallery", "layout": "gallery"},
            {"name": "Table", "layout": "table"},
        ],
    },
}


# ---- write plans --------------------------------------------------------------

def _now(now_ms: int | None) -> int:
    return now_ms if now_ms is not None else int(time.time() * 1000)


def create_plan(maps: dict, template: str, by: str, name: str | None = None,
                now_ms: int | None = None) -> tuple[str, dict]:
    """A database from a template: a fresh db id; the template's property ids are kept."""
    if template not in TEMPLATES:
        raise DbRefusal(f"no template {template!r}; there are: {', '.join(TEMPLATES)}")
    t, now, db = TEMPLATES[template], _now(now_ms), new_id("d")
    writes: dict = {m: {} for m in MAPS}
    writes["dbs"][db] = {"name": name or t["name"], "order": (len(maps.get("dbs") or {}) + 1) * GAP,
                         "template": template, "created": now, "by": by}
    for i, p in enumerate(t["props"]):
        writes["props"][key(db, p["id"])] = {**{k: v for k, v in p.items() if k != "id"}, "order": (i + 1) * GAP}
    for i, v in enumerate(t["views"]):
        writes["views"][key(db, new_id("v"))] = {**v, "order": (i + 1) * GAP}
    return db, writes


def _allowed(prop: dict) -> str:
    t = prop["type"]
    if prop.get("options"):
        return "one of: " + ", ".join(o["name"] for o in prop["options"])
    return {"number": "a number", "checkbox": "true or false", "url": "an http(s):// link",
            "email": "an email address", "date": "a date YYYY-MM-DD (or YYYY-MM-DDTHH:MM)",
            "person": "Matrix ids like @name:server", "relation": "row ids",
            "files": "[{name, url}]"}.get(t, "text")


def cell_writes(d: dict, row: str, values: dict, by: str, now_ms: int | None = None) -> dict:
    """{prop id: raw value} → the cells map writes; every value is checked before any is planned."""
    by_id, now, out = {p["id"]: p for p in d["props"]}, _now(now_ms), {}
    for pid, raw in values.items():
        p = by_id.get(pid)
        if p is None:
            raise DbRefusal(f"no property {pid!r} in this database. Nothing was written.")
        if p["type"] in AUTO:
            raise DbRefusal(f"{p['name']} is computed ({p['type']}), never written. Nothing was written.")
        n = normalize(p, raw)
        if n is UNFIT:
            raise DbRefusal(f"{p['name']}: {raw!r} does not fit a {p['type']} property — expected "
                            f"{_allowed(p)}. Nothing was written.")
        out[key(d["id"], row, pid)] = None if n is None else {"v": n, "updated": now, "by": by}
    return out


def add_row_plan(d: dict, by: str, values: dict | None = None, now_ms: int | None = None) -> tuple[str, dict]:
    now, row = _now(now_ms), new_id("r")
    last = d["rows"][-1]["order"] if d["rows"] else 0
    cells = {k: v for k, v in cell_writes(d, row, values or {}, by, now).items() if v is not None}
    return row, {"rows": {key(d["id"], row): {"order": last + GAP, "created": now, "by": by}}, "cells": cells}


def import_plan(d: dict, records: list[dict], by: str, now_ms: int | None = None) -> tuple[list[str], dict]:
    """Many rows at once, appended in order; one refused value refuses the whole import."""
    now, last = _now(now_ms), d["rows"][-1]["order"] if d["rows"] else 0
    ids, writes = [], {"rows": {}, "cells": {}}
    for i, values in enumerate(records):
        row = new_id("r") + f"{i:x}"
        try:
            cells = cell_writes(d, row, values, by, now)
        except DbRefusal as exc:
            raise DbRefusal(f"record {i + 1}: {exc}") from None
        writes["rows"][key(d["id"], row)] = {"order": last + (i + 1) * GAP, "created": now, "by": by}
        writes["cells"].update({k: v for k, v in cells.items() if v is not None})
        ids.append(row)
    return ids, writes


def move_plan(d: dict, row: str, group_prop: dict, group_id, by: str, now_ms: int | None = None) -> dict:
    """Moving a card on a board = writing its group value."""
    if group_prop["type"] == "multi_select":
        raise DbRefusal(f"{group_prop['name']} holds several values, so a card cannot be moved between its "
                        "groups; set the value with update instead. Nothing was written.")
    return {"cells": cell_writes(d, row, {group_prop["id"]: group_id}, by, now_ms)}


# ---- names people use → ids ---------------------------------------------------

def _fold(s) -> str:
    return " ".join(str(s).split()).casefold()


def resolve_db(maps: dict, name_or_id: str | None) -> dict:
    dbs = list_dbs(maps)
    if not dbs:
        raise DbRefusal("this room has no databases yet; create one with `create --template ...`")
    if not name_or_id or name_or_id == "-":
        if len(dbs) == 1:
            return read_db(maps, dbs[0]["id"])
        raise DbRefusal("this room has several databases; name one: " + ", ".join(x["name"] for x in dbs))
    hit = [x for x in dbs if x["id"] == name_or_id] or [x for x in dbs if _fold(x["name"]) == _fold(name_or_id)]
    if len(hit) != 1:
        why = "several databases are" if hit else "no database is"
        raise DbRefusal(f"{why} named {name_or_id!r}; the room has: "
                        + ", ".join(f"{x['name']} ({x['id']})" for x in dbs))
    return read_db(maps, hit[0]["id"])


def resolve_prop(d: dict, name: str) -> dict:
    hit = ([p for p in d["props"] if p["id"] == name]
           or [p for p in d["props"] if _fold(p["name"]) == _fold(name)])
    if len(hit) != 1:
        raise DbRefusal(f"no property {name!r}; this database has: " + ", ".join(p["name"] for p in d["props"]))
    return hit[0]


def resolve_view(d: dict, name: str | None, layout: str | None = None) -> dict:
    views = [v for v in d["views"] if layout is None or v["layout"] == layout]
    if not views:
        raise DbRefusal(f"this database has no {layout or ''} view".replace("  ", " "))
    if not name:
        return views[0]
    hit = [v for v in views if v["id"] == name] or [v for v in views if _fold(v["name"]) == _fold(name)]
    if len(hit) != 1:
        raise DbRefusal(f"no view {name!r}; this database has: " + ", ".join(v["name"] for v in views))
    return hit[0]


def title_prop(d: dict) -> dict | None:
    return next((p for p in d["props"] if p["type"] == "title"), None)


def resolve_row(d: dict, ref: str) -> dict:
    """A row by its id, or by its title (case-blind) when that names exactly one."""
    hit = [r for r in d["rows"] if r["id"] == ref]
    tp = title_prop(d)
    if not hit and tp:
        hit = [r for r in d["rows"] if _fold(display_value(value_of(d, r, tp), tp)) == _fold(ref)]
    if len(hit) != 1:
        if hit:
            raise DbRefusal(f"{len(hit)} rows are titled {ref!r}; name one by id: "
                            + ", ".join(r["id"] for r in hit))
        raise DbRefusal(f"no row {ref!r} (by id or title) in this database")
    return hit[0]


def parse_input(prop: dict, text: str):
    """A value as typed on a command line → what normalize() takes. Option names match
    case-blind; persons, multi-selects and relations split on commas; `A..B` is a date range."""
    text = text.strip()
    if text == "":
        return None
    t = prop["type"]
    if t in ("select", "status", "multi_select"):
        parts = [x.strip() for x in text.split(",")] if t == "multi_select" else [text]
        out = []
        for x in parts:
            hit = [o["id"] for o in prop.get("options") or [] if _fold(o["name"]) == _fold(x) or o["id"] == x]
            if len(hit) != 1:
                raise DbRefusal(f"{prop['name']}: {x!r} is not an option — {_allowed(prop)}. Nothing was written.")
            out.append(hit[0])
        return out if t == "multi_select" else out[0]
    if t in ("person", "relation"):
        return [x.strip() for x in text.split(",") if x.strip()]
    if t == "checkbox":
        low = text.lower()
        return {"yes": True, "y": True, "1": True, "no": False, "n": False, "0": False}.get(low, low)
    if t == "date" and ".." in text:
        start, end = (x.strip() for x in text.split("..", 1))
        return {"start": start, "end": end}
    return text


def assignments(d: dict, pairs: list[str]) -> dict:
    """['Prop=Value', ...] → {prop id: raw value}, by property name."""
    out = {}
    for pair in pairs:
        name, eq, value = pair.partition("=")
        if not eq:
            raise DbRefusal(f"{pair!r} is not Property=Value")
        p = resolve_prop(d, name.strip())
        out[p["id"]] = parse_input(p, value)
    return out


def group_target(d: dict, prop: dict, to: str):
    """A board column named as people see it → the value a move writes (None for "No …")."""
    if _fold(to) in ("none", "no value", _fold(f"No {prop['name']}")):
        return None
    if prop["type"] == "person":
        return to.strip()
    hit = [o["id"] for o in prop.get("options") or [] if o["id"] == to or _fold(o["name"]) == _fold(to)]
    if len(hit) != 1:
        raise DbRefusal(f"no column {to!r} on this board; it has: "
                        + ", ".join([o["name"] for o in prop.get("options") or []] + [f"No {prop['name']}"]))
    return hit[0]


# ---- what a reader sees -------------------------------------------------------

def view_json(d: dict, view: dict, name: str = "") -> dict:
    """A view's rows with display values keyed by property name; a board's rows also in its groups."""
    hidden = set(view.get("hidden") or [])
    props = [p for p in d["props"] if p["id"] not in hidden]
    rows = query(d, view)
    out = {"db": d["id"], "name": name, "view": {"id": view["id"], "name": view["name"], "layout": view["layout"]},
           "props": [{"id": p["id"], "name": p["name"], "type": p["type"]} for p in props],
           "rows": [{"id": r["id"], "values": {p["name"]: display_value(value_of(d, r, p), p) for p in props}}
                    for r in rows]}
    if view["layout"] == "board":
        out["groups"] = [{"id": g["id"], "name": g["name"], "rows": [r["id"] for r in g["rows"]]}
                         for g in groups(d, view, rows)]
    return out


def render_view(v: dict) -> str:
    """view_json as text: a table, or a board's groups."""
    names = [p["name"] for p in v["props"]]
    by_id = {r["id"]: r for r in v["rows"]}

    def line(r: dict) -> str:
        return "  ".join([r["id"]] + [f"{n}: {r['values'][n]}" for n in names if r["values"].get(n)])

    head = f"# {v['name'] or v['db']} — {v['view']['name']} ({v['view']['layout']}, {len(v['rows'])} rows)"
    if "groups" in v:
        body = []
        for g in v["groups"]:
            body.append(f"## {g['name']} ({len(g['rows'])})")
            body += [f"  {line(by_id[i])}" for i in g["rows"]]
        return "\n".join([head] + body)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(["id"] + names)
    for r in v["rows"]:
        w.writerow([r["id"]] + [r["values"].get(n, "") for n in names])
    return head + "\n" + buf.getvalue().rstrip("\n")


# ---- CSV → records ------------------------------------------------------------

MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
MONTH_DATE_RE = re.compile(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?$")


def csv_date(text: str, year: int | None):
    """"Sep 25" (with a year given) or "Sep 25, 2026" → "2026-09-25"; anything else as it was."""
    m = MONTH_DATE_RE.match(text.strip())
    if not m or m.group(1).lower() not in MONTHS:
        return text
    y = int(m.group(3)) if m.group(3) else year
    if y is None:
        raise DbRefusal(f"the date {text!r} has no year; pass --year")
    return f"{y:04d}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"


def csv_records(d: dict, text: str, *, header_row: int = 1, mapping: list[str] | None = None,
                year: int | None = None) -> tuple[list[dict], dict]:
    """CSV text → ({prop id: raw value} per data row, report). Headers match property names
    case-blind; `mapping` entries 'CSV header=Property' name the rest (a header prefix is
    enough when it names one column). Unmatched headers are reported, never created."""
    table = list(csv.reader(io.StringIO(text)))
    if header_row < 1 or header_row > len(table):
        raise DbRefusal(f"--header-row {header_row}: the file has {len(table)} rows")
    headers = [" ".join(h.split()) for h in table[header_row - 1]]
    cols: dict[int, dict] = {}
    for m in mapping or []:
        src, eq, dst = m.partition("=")
        if not eq:
            raise DbRefusal(f"--map {m!r} is not 'CSV header=Property'")
        want = _fold(src)
        exact = [i for i, h in enumerate(headers) if _fold(h) == want]
        hit = exact or [i for i, h in enumerate(headers) if want and _fold(h).startswith(want)]
        if len(hit) != 1:
            raise DbRefusal(f"--map {m!r}: {'several' if hit else 'no'} CSV headers match {src.strip()!r}; "
                            "the headers are: " + " | ".join(h for h in headers if h))
        cols[hit[0]] = resolve_prop(d, dst.strip())
    for i, h in enumerate(headers):
        if i not in cols and h:
            p = next((p for p in d["props"] if _fold(p["name"]) == _fold(h) or p["id"] == h), None)
            if p:
                cols[i] = p
    for i, p in cols.items():
        if p["type"] in AUTO:
            raise DbRefusal(f"{headers[i]!r} maps to {p['name']}, which is computed and never written")
    records, skipped_blank = [], 0
    for line in table[header_row:]:
        rec = {}
        for i, p in cols.items():
            raw = line[i] if i < len(line) else ""
            if raw.strip() == "":
                continue
            if p["type"] == "date":
                raw = csv_date(raw, year)
            rec[p["id"]] = parse_input(p, raw)
        if rec:
            records.append(rec)
        else:
            skipped_blank += 1
    report = {"columns": {headers[i]: p["name"] for i, p in sorted(cols.items())},
              "unmatched_headers": [h for i, h in enumerate(headers) if h and i not in cols],
              "blank_rows_skipped": skipped_blank}
    return records, report
