"""The local hold for an owner pending question: one JSON record per ask under
`<workspace>/state/pending-questions-outbox/`, written whole in one rename before anything
else is done with the question, and a close record under its `closed/` when an answer or
a closure arrives while no store can take it. Core-owned and dependency-light: a checkout
with no store skill still asks, lists and closes through this module alone; the skill's
store replays and deletes through it too, never by a recipe of its own.

Record: {"question": {ask_id, question, context, asked_at, default_action, reason,
options, priority}, "sent": "<**Sent:** line>", "saved_at": "<iso>"}. The file stem is
the ask id and must equal the record's `question.ask_id`: an id outside ASK_ID_RE is
refused before any path is built, and a file whose stem or inner id does not match is
skipped — named on stderr, never deleted. Deletion removes only the enumerated file,
and only once it resolves inside the outbox directory.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from result_markers import neutralize_markers
from workspace_default import status_path

OUTBOX_DIR = "pending-questions-outbox"
CLOSED_DIR = "closed"
# Marks that this workspace's owner has been told once where the questions database lives;
# its presence is also the sign a room store was used, so losing the store is an outage.
ROOM_INTRODUCED = "pending-questions-db-introduced"
ASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
TERMINAL = ("Answered", "Resolved")


class BadAskId(ValueError):
    """An ask id no path may be built from."""


def safe_ask_id(ask_id) -> str:
    if not isinstance(ask_id, str) or not ASK_ID_RE.match(ask_id):
        raise BadAskId(f"ask id {ask_id!r} is not [A-Za-z0-9][A-Za-z0-9._-]{{0,119}}")
    return ask_id


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def one_line_title(question: str) -> str:
    return " ".join(neutralize_markers(question or "").split())[:120] or "(empty question)"


def room_was_used(workspace) -> bool:
    return status_path(ROOM_INTRODUCED, Path(workspace)).exists()


def mark_room_used(workspace, ask_id: str) -> None:
    """The store's first successful ask writes this; from then on, no store is an outage."""
    p = status_path(ROOM_INTRODUCED, Path(workspace))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(safe_ask_id(ask_id) + "\n", encoding="utf-8")


def _write_whole(path: Path, record: dict) -> Path:
    """Appear whole in one rename; a crash mid-write leaves nothing half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def _skip(path: Path, why: str) -> None:
    print(f"pending-questions outbox: {path.name} {why}; left in place", file=sys.stderr)


class Outbox:
    """The held questions and the local close records of one workspace."""

    def __init__(self, workspace):
        self.dir = status_path(OUTBOX_DIR, Path(workspace))
        self.closed_dir = self.dir / CLOSED_DIR

    def path(self, ask_id: str) -> Path:
        return self.dir / f"{safe_ask_id(ask_id)}.json"

    def close_path(self, ask_id: str) -> Path:
        return self.closed_dir / f"{safe_ask_id(ask_id)}.json"

    def save(self, question: dict, sent_line: str, now: Optional[float] = None) -> Path:
        """Hold `question` (its dict shape; `ask_id` names the file) with its delivery record."""
        ask_id = safe_ask_id((question or {}).get("ask_id"))
        record = {"question": dict(question), "sent": sent_line,
                  "saved_at": _iso(datetime.now(timezone.utc).timestamp() if now is None else now)}
        return _write_whole(self.path(ask_id), record)

    def _contained(self, path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(self.dir.resolve()) and not path.is_symlink()
        except OSError:
            return False

    def _read(self, path: Path, inner_key: str) -> Optional[dict]:
        """The record at `path` when its stem is a safe ask id that the record itself names."""
        if not ASK_ID_RE.match(path.stem):
            _skip(path, "is not named by an ask id")
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            inner = d[inner_key] if inner_key else d
            if not isinstance(d, dict) or not isinstance(inner, dict):
                raise TypeError("not an object")
            if inner.get("ask_id") != path.stem:
                raise ValueError(f"names ask id {inner.get('ask_id')!r}")
        except (OSError, ValueError, KeyError, TypeError) as e:
            _skip(path, f"unreadable ({e})")
            return None
        return d

    def _files(self, d: Path) -> list:
        return sorted(p for p in d.glob("*.json") if p.is_file() and self._contained(p)) if d.is_dir() else []

    def entries(self) -> list:
        """Every held record, oldest name first: {"ask_id", "question", "sent", "saved_at", "path"}."""
        out = []
        for p in self._files(self.dir):
            d = self._read(p, "question")
            if d is not None:
                out.append({"ask_id": p.stem, "question": d["question"], "sent": str(d.get("sent") or ""),
                            "saved_at": str(d.get("saved_at") or ""), "path": p})
        return out

    def _unlink(self, path: Path) -> None:
        if self._contained(path):
            path.unlink(missing_ok=True)

    def delete(self, ask_id: str) -> None:
        self._unlink(self.path(ask_id))

    def close(self, ask_id: str, status: str, note: str = "", now: Optional[float] = None) -> Path:
        """Record a closure no store could take: replayed by the next reconcile with a store."""
        if status not in TERMINAL:
            raise ValueError(f"a close is Answered or Resolved, not {status!r}")
        at = datetime.now(timezone.utc).timestamp() if now is None else now
        return _write_whole(self.close_path(ask_id),
                            {"ask_id": safe_ask_id(ask_id), "status": status, "at": _iso(at), "note": note})

    def closes(self) -> dict:
        """ask id -> close record, for every local close not yet replayed."""
        out = {}
        for p in self._files(self.closed_dir):
            d = self._read(p, "")
            if d is not None and d.get("status") in TERMINAL:
                out[p.stem] = {**d, "path": p}
        return out

    def delete_close(self, ask_id: str) -> None:
        self._unlink(self.close_path(ask_id))


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


def local_done_count(workspace) -> int:
    return len(Outbox(workspace).closes())
