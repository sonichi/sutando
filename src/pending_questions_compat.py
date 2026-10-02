"""TRANSITIONAL — the only code that reads the legacy per-host pending-questions file.

`ingest_legacy_file_entries(workspace, host, store)` copies each open entry an older
head wrote to this host's file — one carrying an **Ask id:** line and a settled
**Sent:** record — into this host's row of the room database through the normal
add_row path (host-tagged, resumable), then marks the file entry moved in place
(`**Status:** moved — as row q-…`). It changes nothing else in the file, and no
other module may read the file's active region; `pending_questions_store.reconcile`
is its single call site. A second pass finds the entries moved and does nothing.

Delete this module, that call, and tests/pending-questions-compat-ingest.test.py under
the policy in docs/migration-transition-window.md: ~30 days of zero source-side
writes, observed as the last mutation of every host's legacy file (`python3
src/pending_questions_compat.py report`). The clock starts at the NEWEST mtime across
hosts, and the ingest's own moved marks are mutations too, so it runs conservative.
"""
from __future__ import annotations

import base64
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

import pending_questions_ledger as ledger
from pending_questions_md import DIVIDER_RE, active_region, mask_markup
from pending_questions_store import Question, StoreError, question_body, row_body, row_id, safe_body

LEGACY_FILE = "pending-questions.md"
ASK_ID_LINE_RE = re.compile(r"^\*\*Ask id:\*\*[ \t]+(\S+)[ \t]*$", re.MULTILINE)
_STATUS_LINE_RE = re.compile(r"^\*\*Status:\*\*.*$", re.MULTILINE)
_SECTION_RE = re.compile(r"^## ", re.MULTILINE)
_HEADING_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z — ")
_PLACEHOLDER_RE = re.compile(r"^\*\*Sent:\*\* \(sending \S+\)$", re.MULTILINE)
_SENT_RE = re.compile(r"^\*\*Sent:\*\*.*$", re.MULTILINE)
_FIELDS_RE = re.compile(r"^<!-- pq-fields: ([A-Za-z0-9+/=]+) -->[ \t]*$", re.MULTILINE)
# Status words of an entry that still wants an answer; `moved` is one this ingest already copied.
OPEN_WORDS = ("unanswered", "waiting", "open")
MOVED = "moved"


def legacy_file_path(workspace, host: str) -> Path:
    """This host's legacy file, via the repo's per-host convention."""
    from util_paths import personal_path  # noqa: PLC0415
    p = Path(personal_path(LEGACY_FILE, Path(workspace)))
    return p if p.exists() else Path(workspace) / "hosts" / host / LEGACY_FILE


def _sections(text: str):
    """(start, end) of each `## ` section of the active region."""
    text = active_region(text)
    starts = [m.start() for m in _SECTION_RE.finditer(text)]
    div = DIVIDER_RE.search(mask_markup(text))
    stop = div.start() if div else len(text)
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        out.append((s, min(e, stop) if s < stop else e))
    return out


def _question_from_fields(ask_id: str, section: str) -> Optional[Question]:
    m = _FIELDS_RE.search(section)
    if not m:
        return None
    try:
        d = json.loads(base64.b64decode(m.group(1)))
        return Question.from_dict({**d, "ask_id": ask_id})
    except (ValueError, KeyError, TypeError):
        return None


def legacy_entries(text: str) -> list:
    """Active-region entries carrying one ask id and a status line, with the status word,
    the settled **Sent:** line (None while still being asked) and the structured fields."""
    out = []
    for s, e in _sections(text):
        sec = text[s:e]
        ids, st = ASK_ID_LINE_RE.findall(sec), _STATUS_LINE_RE.search(sec)
        if len(ids) != 1 or not st:
            continue
        word = st.group(0)[len("**Status:**"):].strip().lower()
        sent = _SENT_RE.search(sec)
        head, _, body = sec[3:].partition("\n")
        out.append({"ask_id": ids[0], "title": _HEADING_TS_RE.sub("", head.strip()), "body": body.strip(),
                    "open": not word or word.startswith(OPEN_WORDS), "moved": word.startswith(MOVED),
                    "sent": sent.group(0) if sent and not _PLACEHOLDER_RE.match(sent.group(0)) else None,
                    "question": _question_from_fields(ids[0], sec)})
    return out


def _mark_moved(path: Path, ask_id: str, row: str) -> Optional[str]:
    def _do(old: str) -> str:
        hits = [(s, e) for s, e in _sections(old) if ASK_ID_LINE_RE.findall(old[s:e]) == [ask_id]]
        if len(hits) != 1:
            raise ledger.LedgerError(f"ask id {ask_id!r} names {len(hits)} entries, expected 1")
        s, e = hits[0]
        m = _STATUS_LINE_RE.search(old, s, e)
        if not m:
            raise ledger.LedgerError(f"entry {ask_id!r} has no **Status:** line")
        return old[:m.start()] + f"**Status:** {MOVED} — as row {row}" + old[m.end():]
    return ledger.update(path, _do)


def ingest_legacy_file_entries(workspace, host: Optional[str], store) -> tuple:
    """(ask ids moved, errors). Open, settled entries with no row of this host get one; an
    entry whose row already exists is only marked. Nothing is ever retired or re-opened."""
    moved, errors = [], []
    path = legacy_file_path(workspace, host or "")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return moved, errors
    except OSError as e:
        return moved, [f"legacy file: {e}"]
    todo = [e for e in legacy_entries(text) if e["open"] and not e["moved"]]
    if not todo:
        return moved, errors
    try:
        have = {r["ask_id"] for r in store.entries()}
    except Exception as e:  # noqa: BLE001
        return moved, [f"legacy ingest: rows unreadable ({type(e).__name__}: {e})"]
    for e in todo:
        aid, q = e["ask_id"], e["question"]
        try:
            if aid not in have:
                if not e["sent"]:
                    continue  # still being asked by a live run of an older head
                body = (question_body(q, e["sent"]) if q is not None
                        else row_body(safe_body(e["body"]), None, None, (), e["sent"]))
                store.insert_raw(aid, q.question if q is not None else e["title"], body,
                                 q.priority if q is not None else "Medium")
                if not store.complete(aid):
                    raise StoreError("the row is still incomplete")
            err = _mark_moved(path, aid, row_id(aid))
            if err:
                raise StoreError(err)
            moved.append(aid)
        except Exception as ex:  # noqa: BLE001 — one entry's failure leaves the rest to try
            errors.append(f"legacy {aid}: {type(ex).__name__}: {ex}")
    return moved, errors


def report(workspace) -> list:
    """Per host directory: (host, path, last mutation epoch | None). A host without the file
    is reported, not skipped; a root-level legacy file is reported as host ''."""
    ws = Path(workspace)
    out = []
    for d in sorted(p for p in (ws / "hosts").glob("*") if p.is_dir()) if (ws / "hosts").is_dir() else []:
        f = d / LEGACY_FILE
        out.append((d.name, f, f.stat().st_mtime if f.exists() else None))
    root = ws / LEGACY_FILE
    if root.exists():
        out.append(("", root, root.stat().st_mtime))
    return out


def main(argv=None) -> int:
    import argparse  # noqa: PLC0415
    ap = argparse.ArgumentParser(description="Report the last mutation of each host's legacy file.")
    ap.add_argument("command", choices=("report",))
    ap.add_argument("--workspace", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.workspace is None:
        from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
        args.workspace = resolve_workspace(migrate=False)
    rows, newest = report(args.workspace), None
    for host, path, mtime in rows:
        if mtime is None:
            print(f"{host or '(root)'}: no legacy file at {path}")
            continue
        newest = max(newest or 0, mtime)
        print(f"{host or '(root)'}: last mutation of the legacy file {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(mtime))} {path}")
    if newest is None:
        print("no legacy file on any host")
    else:
        print(f"30-day clock starts at the newest: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(newest))} "
              f"({(time.time() - newest) / 86400:.1f} days ago)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
