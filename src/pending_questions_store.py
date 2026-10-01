"""Where an owner pending question is kept: one Question, two stores with one
contract, and the fail-open policy between them.

The contract, identical for both stores: `insert` is idempotent on the ask id,
`stamp` replaces the entry's one `**Sent:**` placeholder, `set_status` names one
of STATUSES (or a free note in the file), `open_entries` lists what still wants
an answer, each with the body the reminder reads its `**Sent:**` record from.

FileStore keeps pending-questions.md through pending_questions_ledger, the file's
only writer. RoomDbStore keeps the "Pending questions" database through an
injected DbClient; it owns the schema, the row page and the ask-id-to-row map,
and never names or locates the capability behind the client — the adapter
injects that, and ScriptDbClient runs whatever script the adapter hands it.

write_question(): the room database when one is injected; else, or when it fails,
the file, with the failure returned for the caller to report. resync(): file
entries carrying an ask id are copied into the database (an existing row is left
as it is) and the file entry's status then names the row, so it is not open twice.
"""
from __future__ import annotations

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


class StoreError(Exception):
    """A store declining or failing to write; the message is the reason."""


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
    def title(self) -> str:
        return one_line_title(self.question)

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


def ledger_entry(q: Question) -> str:
    return "\n".join([entry_heading(q.question, q.asked_at), "", quote_for_ledger(ledger_text(q)), "",
                      "**Status:** open", f"**Ask id:** {q.ask_id}", placeholder(q.ask_id), ""]) + "\n"


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
    hits = [(s, e) for s, e in _sections(text)
            if any(m.group(1) == ask_id for m in ASK_ID_LINE_RE.finditer(text, s, e))]
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
            if any(m.group(1) == q.ask_id for m in ASK_ID_LINE_RE.finditer(old)):
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
        word = status.lower() + (f" — {note}" if note else "")

        def _do(old: str) -> str:
            s, e = _find_section(old, ask_id)
            m = _STATUS_LINE_RE.search(old, s, e)
            if not m:
                raise ledger.LedgerError(f"entry {ask_id!r} has no **Status:** line")
            return old[:m.start()] + f"**Status:** {word}" + old[m.end():]
        self._write(_do)

    def open_entries(self) -> list:
        try:
            text = active_region(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        out = []
        for s, e in _sections(text):
            sec = text[s:e]
            ids = ASK_ID_LINE_RE.findall(sec)
            st = _STATUS_LINE_RE.search(sec)
            if len(ids) != 1 or not st or not st.group(0)[len("**Status:**"):].strip().lower().startswith("open"):
                continue
            head, _, body = sec[3:].partition("\n")
            out.append({"ask_id": ids[0], "title": _HEADING_TS_RE.sub("", head.strip()),
                        "body": body.strip()})
        return out


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
    ],
    "views": [
        {"id": "board", "name": "Board", "layout": "board", "groupBy": "status", "hidden": ["ask_id"]},
        {"id": "table", "name": "Table", "layout": "table"},
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


def _option_id(prop_id: str, name: str) -> str:
    prop = next(p for p in DB_SCHEMA["props"] if p["id"] == prop_id)
    for o in prop["options"]:
        if o["name"].lower() == (name or "").strip().lower():
            return o["id"]
    raise StoreError(f"{prop['name']} has no option {name!r}; it has: "
                     + ", ".join(o["name"] for o in prop["options"]))


class RoomDbStore:
    kind = "room-db"

    def __init__(self, client: DbClient, label: str = "the owner's DM room"):
        self.client, self.label = client, label
        self.last_link: Optional[str] = None

    def where(self, ask_id: str) -> str:
        return f"{DB_SCHEMA['name']} database in {self.label}, row {row_id(ask_id)}"

    def insert_raw(self, ask_id: str, title: str, body: str, priority: str = "Medium",
                   status: str = "Open") -> dict:
        cells = {"name": one_line_title(title), "status": _option_id("status", status),
                 "priority": _option_id("priority", priority), "ask_id": ask_id}
        res = self.client.add_row(DB_SCHEMA, row_id(ask_id), cells, body) or {}
        self.last_link = res.get("link")
        return res

    def insert(self, q: Question) -> Optional[str]:
        return self.insert_raw(q.ask_id, q.question, question_body(q), q.priority).get("link")

    def _row(self, ask_id: str) -> dict:
        r = self.client.row(DB_SCHEMA, row_id(ask_id))
        if not r:
            raise StoreError(f"no row for ask id {ask_id!r} in {DB_SCHEMA['name']}")
        return r

    def stamp(self, ask_id: str, sent_line: str) -> None:
        body, token = self._row(ask_id).get("body") or "", placeholder(ask_id)
        n = body.count(token)
        if n != 1:
            raise StoreError(f"token {token!r} occurs {n} times, expected 1")
        self.client.set_body(DB_SCHEMA, row_id(ask_id), body.replace(token, sent_line, 1))

    def set_status(self, ask_id: str, status: str, note: str = "") -> None:
        self._row(ask_id)
        self.client.set_cells(DB_SCHEMA, row_id(ask_id), {"status": _option_id("status", status)})

    def open_entries(self) -> list:
        out = []
        for r in self.client.rows(DB_SCHEMA):
            cells = r.get("cells") or {}
            if cells.get("status") == "open" and cells.get("ask_id"):
                out.append({"ask_id": cells["ask_id"], "title": cells.get("name") or "",
                            "body": (r.get("body") or "").strip()})
        return out


# ---- policy between them -------------------------------------------------------

@dataclass
class WriteOutcome:
    store: object = None              # the store now holding the entry; None when none does
    link: Optional[str] = None
    db_error: Optional[str] = None    # the room database failed; the file took the entry
    error: Optional[str] = None       # no store took the entry
    notes: list = field(default_factory=list)


def write_question(q: Question, file_store: FileStore, db_store=None) -> WriteOutcome:
    """The room database when injected, else the file; a database failure falls
    back to the file and is reported, never raised."""
    out = WriteOutcome()
    if db_store is not None:
        try:
            out.link = db_store.insert(q)
            out.store = db_store
            return out
        except Exception as e:  # noqa: BLE001 — the file must still take the entry
            out.db_error = f"{type(e).__name__}: {e}"
    try:
        file_store.insert(q)
        out.store = file_store
    except Exception as e:  # noqa: BLE001
        out.error = f"{type(e).__name__}: {e}"
    return out


def file_entry_body(body: str) -> tuple:
    """(request text, sent line) of a FileStore entry body, its quote undone."""
    lines, sent = [], None
    for ln in body.splitlines():
        if ln.startswith("**Sent:**"):
            sent = ln
        elif ln.startswith(">"):
            lines.append(ln[2:] if ln.startswith("> ") else "")
    return "\n".join(lines).strip(), sent


def resync(file_store: FileStore, db_store) -> tuple:
    """Copy each open file entry that carries an ask id and a settled `**Sent:**`
    line into the database, then mark it in the file. (moved ask ids, errors)."""
    moved, errors = [], []
    try:
        entries = file_store.open_entries()
    except OSError as e:
        return moved, [f"read {file_store.path}: {e}"]
    for e in entries:
        request, sent = file_entry_body(e["body"])
        if not sent or _PLACEHOLDER_RE.match(sent):
            continue  # still being asked by a live run; its stamp lands in the file
        try:
            db_store.insert_raw(e["ask_id"], e["title"], row_body(request, None, None, (), sent))
            file_store.set_status(e["ask_id"], "moved",
                                  f"kept in the {DB_SCHEMA['name']} database as row {row_id(e['ask_id'])}")
            moved.append(e["ask_id"])
        except Exception as ex:  # noqa: BLE001 — one entry's failure leaves the rest to try
            errors.append(f"{e['ask_id']}: {type(ex).__name__}: {ex}")
    return moved, errors
