"""Search across a room's collaborative surfaces — pure: records in, ranked hits out.

A record is one searchable unit: a Doc page, an HTML page, a database row or a
sheet cell. Each carries `go`, the relay path(s) that open it, so a voice agent
can go where a hit is. Readers (the relay, the CLI) build records from the
documents they opened; nothing here opens a socket.
"""
from __future__ import annotations

import html as _html
import re

LIMIT = 20
LIMIT_MAX = 50
SNIPPET = 160
QUERY_MAX = 200
TITLE_W, HEADING_W, TEXT_W, PHRASE_BONUS = 4, 2, 1, 3

_HIDDEN = re.compile(r"(?is)<(script|style|template|noscript)\b.*?</\1>|<!--.*?-->")
_BLOCK = re.compile(r"(?i)</?(p|div|section|article|li|ul|ol|h[1-6]|br|hr|tr|td|th|table|header|footer|main|nav|aside|blockquote|pre|button|figure|figcaption|img|input|label|option)\b[^>]*>")
_TAG = re.compile(r"<[^>]+>")
_HTML_HEADING = re.compile(r"(?is)<(h[1-3]|title)\b[^>]*>(.*?)</\1>")
_MD_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def visible_text(html: str) -> str:
    """The words a viewer of the page sees, single-spaced; scripts, styles and tags removed."""
    text = _TAG.sub("", _BLOCK.sub("\n", _HIDDEN.sub(" ", html)))
    return " ".join(_html.unescape(text).split())


def html_headings(html: str) -> list[str]:
    body = _HIDDEN.sub(" ", html)
    out = [" ".join(_html.unescape(_TAG.sub(" ", m.group(2))).split()) for m in _HTML_HEADING.finditer(body)]
    return [h for h in out if h]


def md_headings(text: str) -> list[str]:
    return [m.group(1) for m in _MD_HEADING.finditer(text)]


def words(query: str) -> list[str]:
    return [w for w in query[:QUERY_MAX].lower().split() if w]


def doc_record(page_id: str | None, page_title: str, text: str) -> dict:
    go = ["/surface/doc"] + ([f"/page/{page_id}"] if page_id else [])
    return {"surface": "doc", "kind": f"markdown-{page_id}" if page_id else "markdown",
            "page": page_id, "title": page_title, "headings": md_headings(text), "text": text, "go": go}


def html_record(page_id: str | None, page_title: str, source: str) -> dict:
    go = ["/surface/html"] + ([f"/page/{page_id}"] if page_id else [])
    return {"surface": "html", "kind": f"html-{page_id}" if page_id else "html",
            "page": page_id, "title": page_title, "headings": html_headings(source),
            "text": visible_text(source), "go": go}


def db_records(maps: dict, bodies: dict[str, str]) -> list[dict]:
    """One record per row: the title property is the title; the other values and the page body are text."""
    from room_database import display_value, key, list_dbs, read_db, title_prop, value_of
    out = []
    for db in list_dbs(maps):
        d = read_db(maps, db["id"])
        tp = title_prop(d)
        for row in d["rows"]:
            title = display_value(value_of(d, row, tp), tp) if tp else ""
            values = [display_value(value_of(d, row, p), p) for p in d["props"]
                      if p is not tp and p["type"] not in ("created_by", "edited_by", "created_time", "edited_time")]
            body = bodies.get(key(db["id"], row["id"]), "")
            out.append({"surface": "db", "kind": "db", "page": None, "db": db["id"], "db_name": db["name"],
                        "row": row["id"], "title": title, "headings": [],
                        "text": " · ".join(v for v in values if v) + (f"\n{body}" if body else ""),
                        "go": ["/surface/db", f"/db/{db['id']}/row/{row['id']}"]})
    return out


def sheet_records(rows: dict, cols: dict, cells: dict) -> list[dict]:
    """One record per filled cell, named by its address (B12)."""
    from room_sheet import col_name, ordered
    r_at = {r: i for i, r in enumerate(ordered(rows))}
    c_at = {c: i for i, c in enumerate(ordered(cols))}
    out = []
    for k, v in cells.items():
        r, _, c = k.partition("|")
        value = (v or {}).get("v") if isinstance(v, dict) else None
        if r not in r_at or c not in c_at or not isinstance(value, str) or not value.strip():
            continue
        out.append({"surface": "sheet", "kind": "sheet", "page": None, "title": "",
                    "cell": f"{col_name(c_at[c])}{r_at[r] + 1}", "headings": [], "text": value, "go": []})
    return sorted(out, key=lambda x: (len(x["cell"]), x["cell"]))


def snippet(text: str, ws: list[str], phrase: str) -> str:
    flat = " ".join(text.split())
    low = flat.lower()
    at = low.find(phrase) if phrase in low else min((i for i in (low.find(w) for w in ws) if i >= 0), default=0)
    start = max(0, at - SNIPPET // 3)
    end = min(len(flat), start + SNIPPET)
    return ("…" if start else "") + flat[start:end].strip() + ("…" if end < len(flat) else "")


def score(rec: dict, ws: list[str]) -> int:
    """0 unless every word is somewhere in the record; title and headings count more than text."""
    title = (rec.get("title") or "").lower()
    heads = " ".join(rec.get("headings") or []).lower()
    text = (rec.get("text") or "").lower()
    total = 0
    for w in ws:
        s = (TITLE_W if w in title else 0) + (HEADING_W if w in heads else 0) + (TEXT_W if w in text else 0)
        if not s:
            return 0
        total += s
    phrase = " ".join(ws)
    if len(ws) > 1 and (phrase in title or phrase in heads or phrase in " ".join(text.split())):
        total += PHRASE_BONUS
    return total


def search(records: list[dict], query: str, limit: int = LIMIT) -> list[dict]:
    """Ranked hits, best first; ties keep the records' order (surface, then page order)."""
    ws = words(query)
    if not ws:
        return []
    limit = max(1, min(int(limit), LIMIT_MAX))
    phrase = " ".join(ws)
    scored = [(score(r, ws), i, r) for i, r in enumerate(records)]
    hits = sorted((x for x in scored if x[0]), key=lambda x: (-x[0], x[1]))[:limit]
    out = []
    for s, _, r in hits:
        hit = {k: v for k, v in r.items() if k not in ("text", "headings") and v is not None}
        hit["score"] = s
        hit["snippet"] = snippet(r.get("text") or r.get("title") or "", ws, phrase)
        out.append(hit)
    return out


def render(hits: list[dict], failed: list[dict]) -> str:
    lines = []
    for h in hits:
        where = {"doc": "Doc", "html": "HTML page", "db": f"database {h.get('db_name', '')}",
                 "sheet": "sheet"}.get(h["surface"], h["surface"])
        name = h.get("title") or h.get("cell") or "(untitled)"
        lines.append(f"{where:<24} {name}\n    {h['snippet']}")
    lines += [f"could not search {f['kind']}: {f['error']}" for f in failed]
    return "\n".join(lines) if lines else "no matches"
