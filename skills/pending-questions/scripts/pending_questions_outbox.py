"""The local hold for an owner pending question: one JSON record per ask under
`<workspace>/state/pending-questions-outbox/`, written whole in one rename before anything
else is done with the question, and a close record under its `closed/` when an answer or
a closure arrives while no store can take it. The skill's own policy, over core's generic
record directory (src/local_record.py): the ask-id grammar, both record schemas and the
two markers below are defined here and nowhere else.

Record: {"question": {ask_id, question, context, asked_at, default_action, reason,
options, priority}, "sent": "<**Sent:** line>", "saved_at": "<iso>"}. The file stem is
the ask id and must equal the record's `question.ask_id`: an id outside ASK_ID_RE is
refused before any path is built, and a file whose stem or inner id does not match is
skipped — named on stderr, never deleted. Close record: {"ask_id", "status", "at", "note"}.

Two markers under state/, kept apart: STORE_HISTORY says a room row of this workspace was
confirmed (so a store that cannot be reached later is an outage, never a measured zero);
ROOM_INTRODUCED says the owner was told once where the database lives. A pre-split install
has only the second, so it still counts as history. So does a close record naming no held
question: it is only ever written with the row in view or in outage mode, so it is the
evidence of a row when the marker itself could not be written.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
from local_record import RecordDir, iso as _iso
from result_markers import neutralize_markers
from workspace_default import status_path

OUTBOX_DIR = "pending-questions-outbox"
CLOSED_DIR = "closed"
ROOM_INTRODUCED = "pending-questions-db-introduced"
STORE_HISTORY = "pending-questions-store-history"
ASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
TERMINAL = ("Answered", "Resolved")


class BadAskId(ValueError):
    """An ask id no path may be built from."""


def safe_ask_id(ask_id) -> str:
    if not isinstance(ask_id, str) or not ASK_ID_RE.match(ask_id):
        raise BadAskId(f"ask id {ask_id!r} is not [A-Za-z0-9][A-Za-z0-9._-]{{0,119}}")
    return ask_id


def one_line_title(question: str) -> str:
    return " ".join(neutralize_markers(question or "").split())[:120] or "(empty question)"


def store_history(workspace) -> bool:
    """A room row was confirmed for this workspace once (a marker, or a close record naming no
    held question); losing the store is then an outage."""
    ws = Path(workspace)
    if status_path(STORE_HISTORY, ws).exists() or status_path(ROOM_INTRODUCED, ws).exists():
        return True
    return bool(local_closes(ws)[1])


def mark_store_used(workspace, ask_id: str) -> None:
    """Idempotent: the first confirmed row writes it; later calls leave it as it is."""
    p = status_path(STORE_HISTORY, Path(workspace))
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(safe_ask_id(ask_id) + "\n", encoding="utf-8")


def room_introduced(workspace) -> bool:
    return status_path(ROOM_INTRODUCED, Path(workspace)).exists()


def mark_room_introduced(workspace, ask_id: str) -> None:
    """The ask whose queued message carried the one-time introduction."""
    p = status_path(ROOM_INTRODUCED, Path(workspace))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(safe_ask_id(ask_id) + "\n", encoding="utf-8")


def _held_ident(d: dict):
    inner = d["question"]
    if not isinstance(inner, dict):
        raise TypeError("not an object")
    return inner.get("ask_id")


class Outbox:
    """The held questions and the local close records of one workspace."""

    def __init__(self, workspace):
        self.dir = status_path(OUTBOX_DIR, Path(workspace))
        self.closed_dir = self.dir / CLOSED_DIR
        self._held = RecordDir(self.dir, _held_ident)
        self._closed = RecordDir(self.closed_dir, lambda d: d.get("ask_id"))

    def path(self, ask_id: str) -> Path:
        return self._held.path(safe_ask_id(ask_id))

    def close_path(self, ask_id: str) -> Path:
        return self._closed.path(safe_ask_id(ask_id))

    def save(self, question: dict, sent_line: str, now: Optional[float] = None) -> Path:
        """Hold `question` (its dict shape; `ask_id` names the file) with its delivery record."""
        ask_id = safe_ask_id((question or {}).get("ask_id"))
        record = {"question": dict(question), "sent": sent_line,
                  "saved_at": _iso(datetime.now(timezone.utc).timestamp() if now is None else now)}
        return self._held.write(ask_id, record)

    def entries(self) -> list:
        """Every held record, oldest name first: {"ask_id", "question", "sent", "saved_at", "path"}."""
        out = []
        for name, d, p in self._held.entries():
            if not ASK_ID_RE.match(name):
                print(f"pending-questions outbox: {p.name} is not named by an ask id; left in place", file=sys.stderr)
                continue
            out.append({"ask_id": name, "question": d["question"], "sent": str(d.get("sent") or ""),
                        "saved_at": str(d.get("saved_at") or ""), "path": p})
        return out

    def delete(self, ask_id: str) -> None:
        self._held.delete(safe_ask_id(ask_id))

    def close(self, ask_id: str, status: str, note: str = "", now: Optional[float] = None) -> Path:
        """Record a closure no store could take: replayed by the next reconcile with a store."""
        if status not in TERMINAL:
            raise ValueError(f"a close is Answered or Resolved, not {status!r}")
        at = datetime.now(timezone.utc).timestamp() if now is None else now
        return self._closed.write(safe_ask_id(ask_id),
                                  {"ask_id": ask_id, "status": status, "at": _iso(at), "note": note})

    def closes(self) -> dict:
        """ask id -> close record, for every local close not yet applied to a row."""
        out = {}
        for name, d, p in self._closed.entries():
            if ASK_ID_RE.match(name) and d.get("status") in TERMINAL:
                out[name] = {**d, "path": p}
        return out

    def delete_close(self, ask_id: str) -> None:
        self._closed.delete(safe_ask_id(ask_id))


def waiting_item(ask_id: str, title: str, body: str, asked_at: Optional[float], in_room: bool,
                 priority: Optional[str] = None) -> dict:
    """The one shape every reader lists: ask id, title, a one-line snippet, the body the
    reminder reads its **Sent:** record from, when it was asked, and whether its row exists."""
    lines = [ln.strip() for ln in (body or "").splitlines()
             if ln.strip() and not ln.lstrip().startswith(("#", "**Sent:**"))]
    return {"id": title[:40], "ask_id": ask_id, "title": title, "snippet": (lines[0] if lines else "")[:120],
            "body": body or "", "asked_at": asked_at, "priority": priority, "in_room": in_room}


def held_items(workspace) -> list:
    """Held questions still open (no local close record), as waiting items not yet in the room."""
    ob = Outbox(workspace)
    closed = ob.closes()
    out = []
    for e in ob.entries():
        if e["ask_id"] in closed:
            continue
        q = e["question"]
        body = neutralize_markers(str(q.get("question") or "")).strip()
        if (q.get("context") or "").strip():
            body += "\n\n" + neutralize_markers(str(q["context"])).strip()
        body += "\n\n" + e["sent"]
        asked = q.get("asked_at")
        out.append(waiting_item(e["ask_id"], one_line_title(str(q.get("question") or "")), body,
                                float(asked) if asked else None, False, q.get("priority")))
    return out


def local_closes(workspace) -> tuple:
    """(done, pending_close): local close records split by whether a held entry carries the
    ask id. A close of a held question is a measured closure; one of an unknown id waits for
    the row it names and is neither open nor done."""
    ob = Outbox(workspace)
    held = {e["ask_id"] for e in ob.entries()}
    closes = ob.closes()
    done = sorted(a for a in closes if a in held)
    return done, sorted(a for a in closes if a not in held)
