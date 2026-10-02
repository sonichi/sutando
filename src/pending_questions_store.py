"""Where an owner pending question is kept: one Question, two stores with one
contract, and the fail-open policy between them.

The contract, identical for both stores: `insert` is idempotent on the ask id,
`stamp` replaces the entry's one `**Sent:**` placeholder atomically (a second
stamp of the same entry is refused, under concurrency too), `set_status` names
one of STATUSES, `entries` lists every entry with its status and `open_entries`
the open ones, each with the body the reminder reads its `**Sent:**` record from.

FileStore keeps pending-questions.md through pending_questions_ledger, the file's
only writer. RoomDbStore keeps the "Pending questions" database through an
injected DbClient; it owns the schema, the row page and the ask-id-to-row map,
and never names or locates the capability behind the client — the adapter
injects that, and ScriptDbClient runs whatever script the adapter hands it.

write_question(): the room database when one is injected, and the file always —
the file is the shadow every file-only reader keeps reading, so a database row is
never the only copy. A database failure is returned for the caller to report.
resync(): file entries carrying an ask id that the database lacks are inserted
with their structured fields; a status closed on either side closes the other.
Nothing is ever retired from the file.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

import pending_questions_ledger as ledger
from pending_questions_md import DIVIDER_RE, active_region, mask_markup
from result_markers import neutralize_markers

STATUSES = ("Open", "Answered", "Resolved")
TERMINAL = ("Answered", "Resolved")
# A row whose file transition never committed: the file stays the truth, so
# readers ignore the row and the file entry stays visible.
SUPERSEDED = "Superseded"
# Code never writes Status; it is the owner's. Code's own marks (Recovery, Closed) live in
# separate cells, and an owner's Status is read first, so it survives any merge order.
RECOVERY = "superseded"
OPEN_RAW = [None, "open"]
PRIORITIES = ("High", "Medium", "Low")
APPROVE = "Approve"
# Bold field tokens the ledger's readers act on, wherever they occur in a body.
LEDGER_FIELD_RE = re.compile(
    r"\*\*(?=(?:Status|Options|Asked|Question|Sent|Ask id):\*\*)", re.IGNORECASE)
ASK_ID_LINE_RE = re.compile(r"^\*\*Ask id:\*\*[ \t]+(\S+)[ \t]*$", re.MULTILINE)
_STATUS_LINE_RE = re.compile(r"^\*\*Status:\*\*.*$", re.MULTILINE)
_SECTION_RE = re.compile(r"^## ", re.MULTILINE)
_HEADING_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z — ")
_PLACEHOLDER_RE = re.compile(r"^\*\*Sent:\*\* \(sending \S+\)$", re.MULTILINE)
# A legacy entry the migration moved names its row in its status line, whatever the status.
_MOVED_ROW_RE = re.compile(r"^\*\*Status:\*\*.*?\bas row q-(\S+)", re.MULTILINE)
_FIELDS_RE = re.compile(r"^<!-- pq-fields: ([A-Za-z0-9+/=]+) -->[ \t]*$", re.MULTILINE)
# Status words that still want an answer; `moved` is a legacy entry whose row is open.
OPEN_WORDS = ("unanswered", "waiting", "open", "moved")


class StoreError(Exception):
    """A store declining or failing to write; the message is the reason."""


class GuardFailed(StoreError):
    """A guarded write whose preconditions (Host, Status) did not hold; nothing was written."""

    def __init__(self, ask_id: str, current: dict):
        shown = ", ".join(f"{k}={v}" for k, v in sorted(current.items())) or "no such row"
        super().__init__(f"{ask_id}: left as is ({shown})")
        self.current = current


_BODY_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")


def safe_body(text: str) -> str:
    """Text as a row body may hold it: newlines and tabs kept, every other control character and
    lone surrogate replaced with U+FFFD. Nothing else changes, so custom content survives."""
    return _BODY_UNSAFE.sub("\ufffd", text)


def placeholder(ask_id: str) -> str:
    return f"**Sent:** (sending {ask_id})"


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def one_line_title(question: str) -> str:
    return " ".join(neutralize_markers(question).split())[:120] or "(empty question)"


def neutralize(text: str) -> str:
    """Owner-facing text made inert: result markers, ledger field tokens, comment openers."""
    return LEDGER_FIELD_RE.sub("** ", neutralize_markers(text or "")).replace("<!--", "<! --")


def ordered_options(default_action: Optional[str], options) -> list:
    """(label, consequence) pairs with Approve first; Approve carries the default."""
    opts = [(str(a).strip(), str(b).strip()) for a, b in options or () if str(a).strip()]
    approve = [o for o in opts if o[0].lower() == APPROVE.lower()]
    rest = [o for o in opts if o[0].lower() != APPROVE.lower()]
    if approve:
        lead = (APPROVE, approve[0][1] or (default_action or "").strip())
    else:
        lead = (APPROVE, (default_action or "").strip() or "yes, go ahead")
    if not rest:
        rest = [("Decline", "no; leave it as it is")]
    return [lead] + rest


@dataclass
class Question:
    ask_id: str
    question: str
    context: Optional[str] = None
    asked_at: float = 0.0
    default_action: Optional[str] = None
    reason: Optional[str] = None
    options: tuple = ()
    priority: str = "Medium"

    @property
    def proposes(self) -> bool:
        return bool((self.default_action or "").strip() or self.options)


# ---- the file ------------------------------------------------------------------

def quote_for_ledger(text: str) -> str:
    """The canonical ledger encoding of owner-facing text: markers and field
    tokens neutralized, comment openers broken, every line block-quoted (no line
    can be a heading, divider, fence or bullet), open backtick runs closed."""
    lines = [f"> {ln}" if ln.strip() else ">" for ln in neutralize(text).strip().splitlines()] or [">"]
    block = "\n".join(lines)
    for _ in range(block.count("`")):
        visible = mask_markup(block)
        i = visible.find("`")
        if i < 0:
            break
        j = i
        while j < len(visible) and visible[j] == "`":
            j += 1
        block += "\n> " + "`" * (j - i)
    return block


def entry_heading(question: str, now: float) -> str:
    return f"## {_iso(now)} — {one_line_title(question)}"


def ledger_text(q: Question) -> str:
    text = q.question.strip()
    if q.context and q.context.strip():
        text += "\n\nContext: " + q.context.strip()
    if q.proposes:
        default = (q.default_action or "").strip() or "none proposed — your call"
        text += "\n\nProposed default action: " + default
        if q.reason and q.reason.strip():
            text += " — " + q.reason.strip()
        text += "\n" + "\n".join(f"{a} -> {b}" for a, b in ordered_options(q.default_action, q.options))
    return text


def fields_line(q: Question) -> str:
    """The question's structured fields, so a file entry rebuilds the same row."""
    data = {"question": q.question, "context": q.context, "default_action": q.default_action,
            "reason": q.reason, "options": [list(o) for o in q.options], "priority": q.priority,
            "asked_at": q.asked_at}
    return "<!-- pq-fields: " + base64.b64encode(json.dumps(data).encode()).decode() + " -->"


def question_from_fields(ask_id: str, section: str) -> Optional[Question]:
    m = _FIELDS_RE.search(section)
    if not m:
        return None
    try:
        d = json.loads(base64.b64decode(m.group(1)))
        return Question(ask_id, str(d["question"]), d.get("context"), float(d.get("asked_at") or 0),
                        d.get("default_action"), d.get("reason"),
                        tuple(tuple(o) for o in d.get("options") or ()), d.get("priority") or "Medium")
    except (ValueError, KeyError, TypeError):
        return None


def ledger_entry(q: Question) -> str:
    return "\n".join([entry_heading(q.question, q.asked_at), "", quote_for_ledger(ledger_text(q)), "",
                      "**Status:** open", f"**Ask id:** {q.ask_id}", fields_line(q),
                      placeholder(q.ask_id), ""]) + "\n"


def legacy_ask_id(title: str, body: str, host: Optional[str] = None, nth: int = 0) -> str:
    """The ask id the migration gives an entry written before ask ids existed. With `host`, the
    host and the entry's occurrence index salt it, so identical entries never share an id."""
    salt = f"{host}\n{nth}\n" if host else ""
    return "legacy-" + hashlib.sha256(f"{salt}{title}\n{body}".encode()).hexdigest()[:12]


def entry_ask_id(section: str) -> Optional[str]:
    """An entry's ask id: its `**Ask id:**` line, or the row a migration moved it to."""
    ids = ASK_ID_LINE_RE.findall(section)
    if len(ids) == 1:
        return ids[0]
    moved = _MOVED_ROW_RE.findall(section)
    return moved[0] if len(ids) == 0 and len(moved) == 1 else None


def status_name(word: str) -> str:
    """A file status word as one of STATUSES."""
    w = (word or "").strip().lower()
    if not w or w.startswith(OPEN_WORDS):
        return "Open"
    return "Answered" if w.startswith("answered") else "Resolved"


def _sections(text: str):
    """(start, end) of each `## ` section of `text`; a section ends at the next one or the divider."""
    starts = [m.start() for m in _SECTION_RE.finditer(text)]
    div = DIVIDER_RE.search(mask_markup(text))
    stop = div.start() if div else len(text)
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        out.append((s, min(e, stop) if s < stop else e))
    return out


def _find_section(text: str, ask_id: str):
    hits = [(s, e) for s, e in _sections(text) if entry_ask_id(text[s:e]) == ask_id]
    if len(hits) != 1:
        raise ledger.LedgerError(f"ask id {ask_id!r} names {len(hits)} entries, expected 1")
    return hits[0]


class FileStore:
    kind = "file"

    def __init__(self, path):
        self.path = Path(path)

    def where(self, ask_id: str) -> str:
        return str(self.path)

    def _write(self, transform) -> None:
        err = ledger.update(self.path, transform)
        if err:
            raise StoreError(err)

    def insert(self, q: Question) -> Optional[str]:
        entry = ledger_entry(q)

        def _do(old: str) -> str:
            if any(entry_ask_id(old[a:b]) == q.ask_id for a, b in _sections(old)):
                return old
            return ledger.with_entry(old, entry)
        self._write(_do)
        return None

    def stamp(self, ask_id: str, sent_line: str) -> None:
        token = placeholder(ask_id)

        def _do(old: str) -> str:
            n = old.count(token)
            if n != 1:
                raise ledger.LedgerError(f"token {token!r} occurs {n} times, expected 1")
            return old.replace(token, sent_line, 1)
        self._write(_do)

    def set_status(self, ask_id: str, status: str, note: str = "") -> None:
        if status not in STATUSES:
            raise StoreError(f"status {status!r} is not one of {', '.join(STATUSES)}")
        word = status.lower() + (f" — {note}" if note else "")

        def _do(old: str) -> str:
            s, e = _find_section(old, ask_id)
            m = _STATUS_LINE_RE.search(old, s, e)
            if not m:
                raise ledger.LedgerError(f"entry {ask_id!r} has no **Status:** line")
            linked = "" if ASK_ID_LINE_RE.search(old, s, e) else f", kept as row {row_id(ask_id)}"
            return old[:m.start()] + f"**Status:** {word}{linked}" + old[m.end():]
        self._write(_do)

    def entries(self) -> list:
        """Every active-region entry carrying an ask id, with its status and, when
        it was written with them, its structured fields."""
        try:
            text = active_region(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        out = []
        for s, e in _sections(text):
            sec = text[s:e]
            ask_id, st = entry_ask_id(sec), _STATUS_LINE_RE.search(sec)
            if not ask_id or not st:
                continue
            head, _, body = sec[3:].partition("\n")
            out.append({"ask_id": ask_id, "title": _HEADING_TS_RE.sub("", head.strip()),
                        "body": body.strip(), "question": question_from_fields(ask_id, sec),
                        "status": status_name(st.group(0)[len("**Status:**"):])})
        return out

    def open_entries(self) -> list:
        return [{k: e[k] for k in ("ask_id", "title", "body")} for e in self.entries()
                if e["status"] == "Open"]

    def archived_ids(self) -> set:
        """Ask ids of linked entries below the divider: closed, whatever their status line says."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        stop = len(active_region(text))
        return {a for s, e in _sections(text) if s >= stop and (a := entry_ask_id(text[s:e]))}

    def linked_ids(self) -> set:
        """Every ask id an entry anywhere in the file carries or names, archive included."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        return {a for s, e in _sections(text) if (a := entry_ask_id(text[s:e]))}


# ---- the room database ---------------------------------------------------------

def _opt(ident: str, name: str, color: str, group: Optional[str] = None) -> dict:
    return {"id": ident, "name": name, "color": color, **({"group": group} if group else {})}


DB_SCHEMA = {
    "id": "pendingq",
    "name": "Pending questions",
    "props": [
        {"id": "name", "name": "Name", "type": "title"},
        {"id": "status", "name": "Status", "type": "status",
         "options": [_opt("open", "Open", "red", "todo"), _opt("answered", "Answered", "blue", "doing"),
                     _opt("resolved", "Resolved", "green", "done")]},
        {"id": "priority", "name": "Priority", "type": "select",
         "options": [_opt("high", "High", "red"), _opt("medium", "Medium", "yellow"),
                     _opt("low", "Low", "gray")]},
        {"id": "ask_id", "name": "Ask id", "type": "text"},
        {"id": "host", "name": "Host", "type": "text"},
        {"id": "recovery", "name": "Recovery", "type": "text"},
        {"id": "closed", "name": "Closed", "type": "text"},
    ],
    "views": [
        {"id": "board", "name": "Board", "layout": "board", "groupBy": "status",
         "hidden": ["ask_id", "host", "recovery", "closed"],
         "filter": [{"prop": "recovery", "op": "empty"}, {"prop": "closed", "op": "empty"}]},
        {"id": "table", "name": "Table", "layout": "table", "hidden": ["host", "recovery", "closed"],
         "filter": [{"prop": "recovery", "op": "empty"}, {"prop": "closed", "op": "empty"}]},
    ],
}


def row_id(ask_id: str) -> str:
    return "q-" + re.sub(r"[^A-Za-z0-9_-]", "-", ask_id)


def _page_text(text: str) -> str:
    """Neutralized, with no line able to open a page heading of its own."""
    return "\n".join("\\" + ln if ln.lstrip().startswith("#") else ln
                     for ln in neutralize(text).strip().splitlines())


def row_body(request: str, default_action: Optional[str], reason: Optional[str], options,
             sent_line: str) -> str:
    """The row page: Request, the proposed default with its reason, the options
    with Approve first, then the delivery record the reminder reads."""
    default = _page_text(default_action or "") or "None proposed — your call."
    if reason and reason.strip():
        default += " — " + _page_text(reason)
    opts = "\n".join(f"**{_page_text(a)}** -> {_page_text(b)}"
                     for a, b in ordered_options(default_action, options))
    return (f"# Request\n\n{_page_text(request)}\n\n# Proposed default action\n\n{default}\n\n"
            f"{opts}\n\n# Delivery\n\n{sent_line}\n")


def question_body(q: Question) -> str:
    request = q.question.strip()
    if q.context and q.context.strip():
        request += "\n\n" + q.context.strip()
    return row_body(request, q.default_action, q.reason, q.options, placeholder(q.ask_id))


class DbClient(Protocol):
    """Rows of one database, by the DATABASE.md shapes; every call ensures the
    database from `schema` first. Failures raise StoreError."""
    def add_row(self, schema: dict, row: str, cells: dict, body: str) -> dict: ...
    def row(self, schema: dict, row: str) -> Optional[dict]: ...
    def rows(self, schema: dict) -> list: ...
    def set_cells(self, schema: dict, row: str, cells: dict) -> None: ...
    def set_body(self, schema: dict, row: str, body: str) -> None: ...
    def stamp(self, schema: dict, row: str, token: str, replacement: str) -> None: ...
    def guarded(self, schema: dict, row: str, cells: dict, expect: dict) -> dict: ...


class ScriptDbClient:
    """A DbClient that runs an injected script: one JSON request on stdin, one
    JSON reply on stdout, bounded by `timeout`; anything else is a StoreError."""

    def __init__(self, argv: list, timeout: float = 90.0):
        self.argv, self.timeout = list(argv), timeout

    def _call(self, req: dict):
        try:
            r = subprocess.run(self.argv, input=json.dumps(req), capture_output=True,
                               text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise StoreError(f"room database: no reply within {self.timeout:.0f}s") from None
        except OSError as e:
            raise StoreError(f"room database: could not run the adapter ({e})") from None
        try:
            reply = json.loads(r.stdout or "")
        except ValueError:
            reply = None
        if r.returncode != 0 or not isinstance(reply, dict) or not reply.get("ok"):
            why = (reply or {}).get("error") if isinstance(reply, dict) else None
            tail = " ".join((r.stderr or "").split())[-200:]
            raise StoreError(f"room database: {why or 'adapter exit ' + str(r.returncode)}"
                             + (f" ({tail})" if tail and not why else ""))
        return reply.get("result")

    def add_row(self, schema, row, cells, body):
        return self._call({"op": "add_row", "schema": schema, "row": row, "cells": cells, "body": body})

    def row(self, schema, row):
        return self._call({"op": "row", "schema": schema, "row": row})

    def rows(self, schema):
        return self._call({"op": "rows", "schema": schema}) or []

    def set_cells(self, schema, row, cells):
        self._call({"op": "set_cells", "schema": schema, "row": row, "cells": cells})

    def set_body(self, schema, row, body):
        self._call({"op": "set_body", "schema": schema, "row": row, "body": body})

    def stamp(self, schema, row, token, replacement):
        self._call({"op": "stamp", "schema": schema, "row": row, "token": token,
                    "replacement": replacement})

    def guarded(self, schema, row, cells, expect):
        return self._call({"op": "guarded", "schema": schema, "row": row, "cells": cells, "expect": expect})


def _option_id(prop_id: str, name: str) -> str:
    prop = next(p for p in DB_SCHEMA["props"] if p["id"] == prop_id)
    for o in prop["options"]:
        if o["name"].lower() == (name or "").strip().lower():
            return o["id"]
    raise StoreError(f"{prop['name']} has no option {name!r}; it has: "
                     + ", ".join(o["name"] for o in prop["options"]))


def owner_status(cells: dict) -> Optional[str]:
    names = {o["id"]: o["name"] for o in DB_SCHEMA["props"][1]["options"]}
    status = names.get(cells.get("status"))
    return status if status in TERMINAL else None


def _mark(cells: dict, prop: str) -> Optional[str]:
    """A mark counts only when the row's own Host wrote it ("<value>@<host>"): two hosts that
    both created one row on stale replicas merge to one Host, and the other's mark is ignored."""
    val, _, tag = str(cells.get(prop) or "").partition("@")
    return val if val and (tag or None) == (cells.get("host") or None) else None


def effective_status(cells: dict) -> str:
    """The owner's terminal Status wins; then the row's Closed mark; then its Recovery mark."""
    closed = _mark(cells, "closed")
    return (owner_status(cells) or (closed if closed in TERMINAL else None)
            or (SUPERSEDED if _mark(cells, "recovery") else "Open"))


def stale_marks(cells: dict) -> list:
    """Code marks the owning host clears: one beside the owner's terminal Status, or one
    another host wrote."""
    return [k for k in ("closed", "recovery") if cells.get(k) and (owner_status(cells) or not _mark(cells, k))]


class RoomDbStore:
    """`lock` is a host-local mkdir lock every stamp and status transition of this
    database takes. `host` is this host's label: rows carry their origin host, and
    a host reconciles only its own (None: a single-host store, every row is its own)."""
    kind = "room-db"

    def __init__(self, client: DbClient, label: str = "the owner's DM room", lock: Optional[Path] = None,
                 host: Optional[str] = None):
        self.client, self.label, self.lock = client, label, Path(lock) if lock else None
        self.host = host
        self._keys: dict = {}

    def _rid(self, ask_id: str) -> str:
        """This host's row for `ask_id`: the existing one it owns, else a new key that carries
        the host, so two hosts' rows for one ask id are two rows, never one shared row."""
        if not self.host:
            return row_id(ask_id)
        if ask_id not in self._keys:
            for r in self.client.rows(DB_SCHEMA) or []:
                c = r.get("cells") or {}
                if c.get("ask_id") and c.get("host") == self.host:
                    self._keys.setdefault(c["ask_id"], r["id"])
        return self._keys.get(ask_id) or f"{row_id(ask_id)}~{hashlib.sha256(self.host.encode()).hexdigest()[:12]}"
        self.last_link: Optional[str] = None

    def where(self, ask_id: str) -> str:
        return f"{DB_SCHEMA['name']} database in {self.label}, row {row_id(ask_id)}"

    def insert_raw(self, ask_id: str, title: str, body: str, priority: str = "Medium",
                   status: str = "Open") -> dict:
        cells = {"name": one_line_title(title), "status": _option_id("status", status),
                 "priority": _option_id("priority", priority), "ask_id": ask_id}
        if self.host:
            cells["host"] = self.host
        res = self.client.add_row(DB_SCHEMA, self._rid(ask_id), cells, body) or {}
        self.last_link = res.get("link")
        return res

    def insert(self, q: Question) -> Optional[str]:
        return self.insert_raw(q.ask_id, q.question, question_body(q), q.priority).get("link")

    def _row(self, ask_id: str) -> dict:
        r = self.client.row(DB_SCHEMA, self._rid(ask_id))
        if not r:
            raise StoreError(f"no row for ask id {ask_id!r} in {DB_SCHEMA['name']}")
        return r

    def _locked(self, fn):
        if self.lock is None:
            raise StoreError("the room database store has no lock; refusing an unserialized write")
        err, result = ledger.under_lock(self.lock, fn)
        if err:
            raise StoreError(err)
        return result

    def stamp(self, ask_id: str, sent_line: str) -> None:
        """One check-and-replace in the client's single call, serialized by `lock`."""
        self._locked(lambda: self.client.stamp(DB_SCHEMA, self._rid(ask_id), placeholder(ask_id), sent_line))

    def _guarded(self, ask_id: str, cells: dict, expect: dict) -> None:
        """Write `cells` only while every `expect` prop holds one of its listed raw values
        (and Host is this host), checked and written in one client call under `lock`."""
        if self.host:
            expect = {"host": [self.host], **expect}
        res = self._locked(lambda: self.client.guarded(DB_SCHEMA, self._rid(ask_id), cells, expect)) or {}
        if not res.get("written"):
            names = {o["id"]: o["name"] for o in DB_SCHEMA["props"][1]["options"]}
            current = {k: names.get(v, v) if k == "status" else v for k, v in (res.get("current") or {}).items()}
            raise GuardFailed(ask_id, {k: v for k, v in current.items() if v is not None})

    def _tag(self, value: str) -> str:
        return f"{value}@{self.host}" if self.host else value

    def close(self, ask_id: str, to: str) -> None:
        """Record a closure made in this host's file, in the Closed cell; Status stays the owner's."""
        if to not in TERMINAL:
            raise StoreError(f"code only closes a row; {to!r} is not Answered or Resolved")
        self._guarded(ask_id, {"closed": self._tag(to)}, {"closed": [None]})

    def supersede(self, ask_id: str) -> None:
        """Mark this host's open row superseded, in the Recovery cell only."""
        self._guarded(ask_id, {"recovery": self._tag(RECOVERY)},
                      {"closed": [None], "status": OPEN_RAW, "recovery": [None]})

    def body_of(self, ask_id: str) -> Optional[str]:
        r = self.client.row(DB_SCHEMA, self._rid(ask_id))
        return (r or {}).get("body") if r else None

    def restore(self, ask_id: str) -> None:
        """Clear this host's Recovery mark; Status is not touched."""
        self._guarded(ask_id, {"recovery": None}, {})

    def clear(self, ask_id: str, props: list) -> None:
        """Remove stale code marks from this host's row; Status is not touched."""
        self._guarded(ask_id, {p: None for p in props}, {})

    def set_status(self, ask_id: str, status: str, note: str = "") -> None:
        self._row(ask_id)
        self.client.set_cells(DB_SCHEMA, self._rid(ask_id), {"status": _option_id("status", status)})

    def status_of(self, ask_id: str) -> Optional[str]:
        r = self.client.row(DB_SCHEMA, self._rid(ask_id))
        return effective_status((r or {}).get("cells") or {}) if r else None

    def entries(self) -> list:
        out = []
        for r in self.client.rows(DB_SCHEMA):
            cells = r.get("cells") or {}
            if cells.get("ask_id") and self.host and cells.get("host") == self.host:
                self._keys.setdefault(cells["ask_id"], r["id"])
            if cells.get("ask_id"):
                out.append({"ask_id": cells["ask_id"], "title": cells.get("name") or "",
                            "body": (r.get("body") or "").strip(), "host": cells.get("host") or None,
                            "recovery": bool(cells.get("recovery")), "stale": stale_marks(cells),
                            "incomplete": (_mark(cells, "recovery") or "") == "incomplete",
                            "status": effective_status(cells)})
        return out

    def open_entries(self) -> list:
        return [{k: e[k] for k in ("ask_id", "title", "body")} for e in self.entries()
                if e["status"] == "Open"]

    def owns(self, row: dict) -> bool:
        return self.host is None or row.get("host") == self.host


# ---- the adapter registration ---------------------------------------------------

REGISTRATION = "pending-questions-store.json"


def registration_path(workspace) -> Path:
    from workspace_default import status_path  # noqa: PLC0415 — heavy loader
    return status_path(REGISTRATION, Path(workspace))


def register_adapter(workspace, adapter: Path) -> None:
    """Record the adapter that made a room-database store, so every later reminder
    pass reconciles through it without a flag on its schedule."""
    path = registration_path(workspace)
    record = json.dumps({"adapter": str(Path(adapter).resolve())}) + "\n"
    try:
        if path.read_text(encoding="utf-8") == record:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger.replace_file(path, record)


def registered_adapter(workspace) -> Optional[str]:
    try:
        return str(json.loads(registration_path(workspace).read_text(encoding="utf-8"))["adapter"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---- policy between them -------------------------------------------------------

@dataclass
class WriteOutcome:
    stores: list = field(default_factory=list)   # every store now holding the entry
    link: Optional[str] = None
    db_error: Optional[str] = None    # the room database failed; the file alone holds it
    error: Optional[str] = None       # the file (the shadow every file reader reads) failed


def write_question(q: Question, file_store: FileStore, db_store=None) -> WriteOutcome:
    """The room database when injected, and the file always; a failure on either
    side is reported, never raised."""
    out = WriteOutcome()
    if db_store is not None:
        try:
            out.link = db_store.insert(q)
            out.stores.append(db_store)
        except Exception as e:  # noqa: BLE001 — the file must still take the entry
            out.db_error = f"{type(e).__name__}: {e}"
    try:
        file_store.insert(q)
        out.stores.append(file_store)
    except Exception as e:  # noqa: BLE001
        out.error = f"{type(e).__name__}: {e}"
    return out


def sent_line_of(body: str) -> Optional[str]:
    m = re.search(r"^\*\*Sent:\*\*.*$", body or "", re.MULTILINE)
    return m.group(0) if m else None


def settled(line: Optional[str]) -> bool:
    return bool(line) and not _PLACEHOLDER_RE.match(line)


def resync(file_store: FileStore, db_store) -> tuple:
    """Bring this host's rows and this host's file level, never retiring a file entry.

    A row is this host's when its Host is this host (every row is created with its
    Host, and only that host writes it); any other row is never judged against this
    host's file. For this host's rows: an open file entry the database lacks is
    inserted; code marks beside the owner's terminal Status are cleared; an entry
    below the divider closes its row; a status closed on either side closes the
    other; a settled `**Sent:**` fills the other side's placeholder; a migration row
    no file entry links is superseded, and one a file entry links is restored. Code
    never writes Status; its marks live in their own cells. (synced ask ids, errors)."""
    synced, errors = [], []
    try:
        entries = file_store.entries()
        linked, archived = file_store.linked_ids(), file_store.archived_ids()
        own = {r["ask_id"]: r for r in db_store.entries() if db_store.owns(r)}
    except Exception as e:  # noqa: BLE001
        return synced, [f"read: {type(e).__name__}: {e}"]

    def note(aid, ex):
        errors.append(str(ex) if isinstance(ex, GuardFailed) else f"{aid}: {type(ex).__name__}: {ex}")

    for aid, r in own.items():
        try:
            if r.get("stale"):
                db_store.clear(aid, r["stale"])
                synced.append(aid)
            if aid in archived and r["status"] not in TERMINAL:
                db_store.close(aid, "Resolved")
                r["status"] = "Resolved"
                synced.append(aid)
            elif aid.startswith("legacy-") and aid not in linked and r["status"] == "Open":
                db_store.supersede(aid)
                r["status"] = SUPERSEDED
                synced.append(aid)
        except Exception as ex:  # noqa: BLE001
            note(aid, ex)
    for e in entries:
        aid, sent = e["ask_id"], sent_line_of(e["body"])
        try:
            if aid not in own:
                q = e["question"]
                if e["status"] != "Open" or q is None or not sent or _PLACEHOLDER_RE.match(sent):
                    continue  # closed, legacy, or still being asked by a live run
                db_store.insert_raw(aid, q.question, question_body(q).replace(placeholder(aid), sent),
                                    q.priority)
                synced.append(aid)
                continue
            row, row_sent = own[aid], sent_line_of(own[aid]["body"])
            if row.get("incomplete"):  # its body commit never landed: resume it from this file entry
                q = e["question"]
                body = (question_body(q).replace(placeholder(aid), sent) if q is not None and sent
                        else row_body(safe_body(e["body"]), None, None, (), "**Sent:** (legacy entry)"))
                db_store.insert_raw(aid, q.question if q is not None else e["title"], body,
                                    q.priority if q is not None else "Medium")
                row["status"], row["body"], row_sent = "Open", body, sent_line_of(body)
                synced.append(aid)
            elif row["status"] == SUPERSEDED:
                db_store.restore(aid)
                row["status"] = "Open"
                synced.append(aid)
            if row["status"] == "Open" and e["status"] in TERMINAL:
                db_store.close(aid, e["status"])
                synced.append(aid)
            elif row["status"] in TERMINAL and e["status"] == "Open":
                file_store.set_status(aid, row["status"], "in the room database")
                synced.append(aid)
            elif sent == placeholder(aid) and settled(row_sent):
                file_store.stamp(aid, row_sent)
                synced.append(aid)
            elif row_sent == placeholder(aid) and settled(sent):
                db_store.stamp(aid, sent)
                synced.append(aid)
        except Exception as ex:  # noqa: BLE001 — one entry's failure leaves the rest to try
            note(aid, ex)
    return list(dict.fromkeys(synced)), errors
