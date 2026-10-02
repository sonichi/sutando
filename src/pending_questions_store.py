"""Where an owner pending question is kept: one Question, one store, one outbox.

RoomDbStore keeps the "Pending questions" database through an injected DbClient;
it owns the schema, the row page and the ask-id-to-row map, and never names or
locates the capability behind the client — the adapter injects that, and
ScriptDbClient runs whatever script the adapter hands it. Rows carry their Host;
a host reads and writes only its own rows. Code never writes Status (the owner's
cell): its own marks live in the Recovery and Closed cells, and `effective_status`
lets the owner's terminal Status win over any of them.

Outbox is the durable local hold for unreachability only: one JSON file per ask,
written atomically before the room write and deleted only once the complete row
is confirmed (`RoomDbStore.complete`). It is never edited; `flush` replays each
entry through the normal add_row path, which resumes a row left incomplete, so a
replay is idempotent by ask id. `reconcile_pending` is every pass's first step: flush the
outbox, then the one transitional ingest of the legacy file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

import pending_questions_ledger as ledger
from result_markers import neutralize_markers
from workspace_default import status_path

STATUSES = ("Open", "Answered", "Resolved")
TERMINAL = ("Answered", "Resolved")
# A row born but not finished carries "incomplete@<host>" in Recovery until its body lands.
INCOMPLETE = "incomplete"
OPEN_RAW = [None, "open"]
PRIORITIES = ("High", "Medium", "Low")
APPROVE = "Approve"
# Bold field tokens the readers of a body act on, wherever they occur.
LEDGER_FIELD_RE = re.compile(
    r"\*\*(?=(?:Status|Options|Asked|Question|Sent|Ask id):\*\*)", re.IGNORECASE)
SENT_LINE_RE = re.compile(r"^\*\*Sent:\*\*.*$", re.MULTILINE)


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
    return _BODY_UNSAFE.sub("�", text)


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

    def to_dict(self) -> dict:
        return {**asdict(self), "options": [list(o) for o in self.options]}

    @classmethod
    def from_dict(cls, d: dict) -> "Question":
        return cls(str(d["ask_id"]), str(d["question"]), d.get("context"), float(d.get("asked_at") or 0),
                   d.get("default_action"), d.get("reason"),
                   tuple(tuple(o) for o in d.get("options") or ()), d.get("priority") or "Medium")


def entry_heading(question: str, now: float) -> str:
    return f"## {_iso(now)} — {one_line_title(question)}"


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


def question_body(q: Question, sent_line: str) -> str:
    request = q.question.strip()
    if q.context and q.context.strip():
        request += "\n\n" + q.context.strip()
    return row_body(request, q.default_action, q.reason, q.options, sent_line)


def sent_line_of(body: str) -> Optional[str]:
    m = SENT_LINE_RE.search(body or "")
    return m.group(0) if m else None


class DbClient(Protocol):
    """Rows of one database, by the DATABASE.md shapes; every call ensures the
    database from `schema` first. Failures raise StoreError."""
    def add_row(self, schema: dict, row: str, cells: dict, body: str) -> dict: ...
    def row(self, schema: dict, row: str) -> Optional[dict]: ...
    def rows(self, schema: dict) -> list: ...
    def set_cells(self, schema: dict, row: str, cells: dict) -> None: ...
    def set_body(self, schema: dict, row: str, body: str) -> None: ...
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
    """The owner's terminal Status wins; then the row's Closed mark; else Open."""
    closed = _mark(cells, "closed")
    return owner_status(cells) or (closed if closed in TERMINAL else None) or "Open"


def stale_marks(cells: dict) -> list:
    """Code marks the owning host clears: one beside the owner's terminal Status, or one
    another host wrote."""
    return [k for k in ("closed", "recovery") if cells.get(k) and (owner_status(cells) or not _mark(cells, k))]


class RoomDbStore:
    """`lock` is a host-local mkdir lock every status transition of this database
    takes. `host` is this host's label: rows carry their origin host, and a host
    reads only its own (None: a single-host store, every row is its own). `link`
    is the database's page in the room, when the adapter knows it."""
    kind = "room-db"

    def __init__(self, client: DbClient, label: str = "the owner's DM room", lock: Optional[Path] = None,
                 host: Optional[str] = None, link: Optional[str] = None):
        self.client, self.label, self.lock = client, label, Path(lock) if lock else None
        self.host, self.link = host, link
        self.last_link: Optional[str] = None
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

    def where(self, ask_id: str) -> str:
        return f"{DB_SCHEMA['name']} database in {self.label}, row {row_id(ask_id)}"

    def insert_raw(self, ask_id: str, title: str, body: str, priority: str = "Medium") -> dict:
        cells = {"name": one_line_title(title), "status": _option_id("status", "Open"),
                 "priority": _option_id("priority", priority), "ask_id": ask_id}
        if self.host:
            cells["host"] = self.host
        res = self.client.add_row(DB_SCHEMA, self._rid(ask_id), cells, body) or {}
        self.last_link = res.get("link")
        return res

    def insert(self, q: Question, sent_line: str) -> dict:
        return self.insert_raw(q.ask_id, q.question, question_body(q, sent_line), q.priority)

    def complete(self, ask_id: str) -> bool:
        """The row is present with a body and no incomplete mark: the confirmation an
        outbox entry is deleted on."""
        r = self.client.row(DB_SCHEMA, self._rid(ask_id))
        if not r or not (r.get("body") or "").strip():
            return False
        return _mark(r.get("cells") or {}, "recovery") != INCOMPLETE

    def _locked(self, fn):
        if self.lock is None:
            raise StoreError("the room database store has no lock; refusing an unserialized write")
        err, result = ledger.under_lock(self.lock, fn)
        if err:
            raise StoreError(err)
        return result

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
        """Record a closure made by code in the Closed cell; Status stays the owner's."""
        if to not in TERMINAL:
            raise StoreError(f"code only closes a row; {to!r} is not Answered or Resolved")
        self._guarded(ask_id, {"closed": self._tag(to)}, {"closed": [None]})

    def clear(self, ask_id: str, props: list) -> None:
        """Remove stale code marks from this host's row; Status is not touched."""
        self._guarded(ask_id, {p: None for p in props}, {})

    def status_of(self, ask_id: str) -> Optional[str]:
        r = self.client.row(DB_SCHEMA, self._rid(ask_id))
        return effective_status((r or {}).get("cells") or {}) if r else None

    def entries(self) -> list:
        """This host's rows (every row of a single-host store), each with its effective status."""
        out = []
        for r in self.client.rows(DB_SCHEMA):
            cells = r.get("cells") or {}
            if not cells.get("ask_id") or not self.owns(cells):
                continue
            self._keys.setdefault(cells["ask_id"], r["id"])
            created = r.get("created")
            out.append({"ask_id": cells["ask_id"], "title": cells.get("name") or "",
                        "body": (r.get("body") or "").strip(), "host": cells.get("host") or None,
                        "priority": cells.get("priority") or None, "stale": stale_marks(cells),
                        "incomplete": _mark(cells, "recovery") == INCOMPLETE,
                        "asked_at": created / 1000 if isinstance(created, (int, float)) else None,
                        "status": effective_status(cells)})
        return out

    def open_entries(self) -> list:
        return [e for e in self.entries() if e["status"] == "Open" and not e["incomplete"]]

    def owns(self, row: dict) -> bool:
        return self.host is None or row.get("host") == self.host


# ---- the outbox -----------------------------------------------------------------

OUTBOX_DIR = "pending-questions-outbox"


class Outbox:
    """One JSON file per ask under `<workspace>/state/pending-questions-outbox/`, holding the
    Question and its delivery record. Written once, deleted once the row is confirmed."""

    def __init__(self, workspace):
        self.dir = status_path(OUTBOX_DIR, Path(workspace))

    def path(self, ask_id: str) -> Path:
        return self.dir / f"{ask_id}.json"

    def save(self, q: Question, sent_line: str, now: Optional[float] = None) -> Path:
        """Appear whole in one rename; a crash mid-write leaves nothing half-written."""
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {"question": q.to_dict(), "sent": sent_line,
                  "saved_at": _iso(datetime.now(timezone.utc).timestamp() if now is None else now)}
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=f".{q.ask_id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path(q.ask_id))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self.path(q.ask_id)

    def delete(self, ask_id: str) -> None:
        self.path(ask_id).unlink(missing_ok=True)

    def entries(self) -> list:
        """Every held (Question, sent line, saved_at), oldest first; an unreadable file is skipped
        and named on stderr, never deleted."""
        out = []
        for p in sorted(self.dir.glob("*.json")) if self.dir.is_dir() else []:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                out.append({"question": Question.from_dict(d["question"]), "sent": str(d.get("sent") or ""),
                            "saved_at": str(d.get("saved_at") or ""), "path": p})
            except (OSError, ValueError, KeyError, TypeError) as e:
                import sys  # noqa: PLC0415
                print(f"pending-questions outbox: {p.name} unreadable ({e}); left in place", file=sys.stderr)
        return out

    def flush(self, store) -> tuple:
        """Replay every held question into `store` through add_row (which resumes a row left
        incomplete) and delete the file once the row is confirmed complete. (flushed ids, errors)."""
        flushed, errors = [], []
        for e in self.entries():
            q = e["question"]
            try:
                store.insert(q, e["sent"] or f"**Sent:** record not kept; held in the outbox since {e['saved_at']}")
                if not store.complete(q.ask_id):
                    raise StoreError("the row is still incomplete after the replay")
                self.delete(q.ask_id)
                flushed.append(q.ask_id)
            except Exception as ex:  # noqa: BLE001 — one entry's failure leaves the rest to try
                errors.append(f"{q.ask_id}: {type(ex).__name__}: {ex}")
        return flushed, errors


def waiting_item(ask_id: str, title: str, body: str, asked_at: Optional[float], in_room: bool,
                 priority: Optional[str] = None) -> dict:
    """The one shape every reader lists: ask id, title, a one-line snippet, the body the
    reminder reads its **Sent:** record from, when it was asked, and whether its row exists."""
    lines = [ln.strip() for ln in (body or "").splitlines()
             if ln.strip() and not ln.lstrip().startswith(("#", "**Sent:**"))]
    return {"id": title[:40], "ask_id": ask_id, "title": title, "snippet": (lines[0] if lines else "")[:120],
            "body": body or "", "asked_at": asked_at, "priority": priority, "in_room": in_room}


def outbox_items(workspace) -> list:
    """Held questions as waiting items, each marked not yet in the room."""
    return [waiting_item(e["question"].ask_id, one_line_title(e["question"].question),
                         question_body(e["question"], e["sent"]), e["question"].asked_at or None, False,
                         e["question"].priority) for e in Outbox(workspace).entries()]


# ---- policy ---------------------------------------------------------------------

@dataclass
class WriteOutcome:
    link: Optional[str] = None
    complete: bool = False            # the row is confirmed present, with its body, not marked
    db_error: Optional[str] = None    # why it is not; the outbox entry stands


def write_question(q: Question, store, sent_line: str) -> WriteOutcome:
    """The room row, born with its delivery record, then confirmed. Failures are reported,
    never raised: the caller's outbox entry already holds the question."""
    out = WriteOutcome()
    if store is None:
        out.db_error = "no room database store"
        return out
    try:
        out.link = (store.insert(q, sent_line) or {}).get("link") or store.link
        out.complete = store.complete(q.ask_id)
        if not out.complete:
            out.db_error = "the row was not confirmed complete"
    except Exception as e:  # noqa: BLE001
        out.db_error = f"{type(e).__name__}: {e}"
    return out


def reconcile_pending(store, workspace, host: Optional[str]) -> dict:
    """Every pass's first step with a reachable store: replay the outbox, clear stale marks on
    this host's rows, then the transitional ingest of the legacy file (its single call site)."""
    flushed, errors = Outbox(workspace).flush(store)
    try:
        for r in store.entries():
            if r["stale"]:
                store.clear(r["ask_id"], r["stale"])
    except Exception as e:  # noqa: BLE001
        errors.append(f"stale marks: {type(e).__name__}: {e}")
    import pending_questions_compat as compat  # noqa: PLC0415 — transitional; see its docstring
    moved, ingest_errors = compat.ingest_legacy_file_entries(workspace, host, store)
    return {"flushed": flushed, "moved": moved, "errors": errors + ingest_errors}


# ---- adapter discovery ------------------------------------------------------------

# The manifest field an installed skill declares its adapter script with.
DECLARATION = "pending_questions_store"


def declared_adapter(skills_dir) -> Optional[Path]:
    """The adapter the first installed skill declares in its manifest; None when none does.
    The script must resolve inside its own skill, since a manifest may come from a third party."""
    for manifest in sorted(Path(skills_dir).glob("*/manifest.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rel = data.get(DECLARATION) if isinstance(data, dict) else None
        if not isinstance(rel, str) or not rel or data.get("enabled") is False:
            continue
        skill = manifest.parent.resolve()
        script = (skill / rel).resolve()
        if script.is_relative_to(skill) and script.is_file():
            return script
    return None


def load_adapter(adapter):
    """The adapter module from its file; None when there is none or it does not load."""
    if not adapter:
        return None
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("pq_store_adapter", str(adapter))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_adapter_store(adapter, workspace) -> tuple:
    """(store | None, why) from an adapter file's `room_store(workspace)`; never raises."""
    if not adapter:
        return None, "no pending-questions store adapter installed"
    try:
        return load_adapter(adapter).room_store(Path(workspace))
    except Exception as e:  # noqa: BLE001 — the outbox alone still holds every question
        return None, f"adapter {adapter} failed to load ({type(e).__name__}: {e})"
