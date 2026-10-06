"""TRANSITIONAL — the only code that reads the legacy per-host pending-questions file.

`ingest_legacy_file_entries(workspace, host, store)` copies each open entry of this
host's file into this host's row of the room database through the normal add_row path
(host-tagged, resumable), then marks the file entry moved in place. Three shapes are
read, every one an older head wrote: a `## ` section carrying an **Ask id:** line and a
settled **Sent:** record (#5003 and this branch's earlier heads; the `(sending …)`
placeholder means a live run is still asking and is left alone); a `## ` section with
no ask id — main's prose, open unless its **Status:** says otherwise — keyed by a digest
of its text; and main's free-form `- **[label, ts]** …` bullets, keyed the same way. A
digest-keyed entry gets a settled Sent line saying its delivery was not recorded. The
file entry is marked moved ONLY after the row is confirmed complete (`store.complete`)
and the store-history marker is committed, so a crash between the row and its body, or
a marker that cannot be written, leaves the entry open for the next pass to finish. It changes nothing else in the file, no other module may read the file's
active region, and `pending_questions_store.reconcile_pending` is its single call site.

Delete this module, that call, and tests/pending-questions-compat-ingest.test.py under
the policy in docs/migration-transition-window.md: ~30 days of zero source-side
writes, observed as the last mutation of every host's legacy file (`python3
skills/pending-questions/scripts/pending_questions_compat.py report`). The clock
starts at the NEWEST mtime across hosts, and the ingest's own moved marks are
mutations too, so it runs conservative.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
HERE = Path(__file__).resolve().parent
for _p in (REPO / "src", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import pending_questions_ledger as ledger
from pending_questions_md import DIVIDER_RE, active_region, mask_markup
from pending_questions_outbox import mark_store_used
from pending_questions_store import Question, StoreError, question_body, row_body, row_id, safe_body

LEGACY_FILE = "pending-questions.md"
ASK_ID_LINE_RE = re.compile(r"^\*\*Ask id:\*\*[ \t]+(\S+)[ \t]*$", re.MULTILINE)
_STATUS_LINE_RE = re.compile(r"^\*\*Status:\*\*.*$", re.MULTILINE)
# A bullet carries its mark at the end of its own line, so its Status is read anywhere on it.
_STATUS_ANY_RE = re.compile(r"\*\*Status:\*\*.*$", re.MULTILINE)
_SECTION_RE = re.compile(r"^## ", re.MULTILINE)
_HEADING_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z — ")
_PLACEHOLDER_RE = re.compile(r"^\*\*Sent:\*\* \(sending \S+\)$", re.MULTILINE)
_SENT_RE = re.compile(r"^\*\*Sent:\*\*.*$", re.MULTILINE)
_FIELDS_RE = re.compile(r"^<!-- pq-fields: ([A-Za-z0-9+/=]+) -->[ \t]*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*-[ \t]+\*\*\[(.+?)\][^\n]*$", re.MULTILINE)
# A heading main's reader treated as organisation or as already settled, never a question.
_ORG_HEADING = re.compile(r"^\s*(?:Q\d+\s*[—–-]\s*)?(?:Open|Pending|Resolved|Done|Answered|Archive)\s*$", re.I)
_INLINE_RESOLVED = re.compile(r"^\s*(?:\d+[.)]\s*)?\[\s*(?:✅\s*)?(?:RESOLVED|DONE|ANSWERED)(?:\s[^\]]*)?\]", re.I)
# Status words of an entry that still wants an answer; `moved` is one this ingest already copied.
OPEN_WORDS = ("unanswered", "waiting", "open")
MOVED = "moved"
LEGACY_PREFIX = "legacy-"


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


def _bullets(text: str):
    """(start, end) of each top-level `- **[label, ts]** …` bullet line of the active region
    that is not inside a `## ` section (those are the section's own text)."""
    region = active_region(text)
    div = DIVIDER_RE.search(mask_markup(region))
    stop = div.start() if div else len(region)
    secs = _sections(text)
    out = []
    for m in _BULLET_RE.finditer(region, 0, stop):
        if not any(s <= m.start() < e for s, e in secs):
            out.append((m.start(), m.end()))
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


def digest_key(text: str) -> str:
    """A stable id for an entry with none of its own: its text with the Status line removed
    (the only line the ingest rewrites) and whitespace normalized."""
    norm = " ".join(_STATUS_ANY_RE.sub("", text).split())
    return LEGACY_PREFIX + hashlib.sha256(norm.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def legacy_sent_line(now: Optional[float] = None) -> str:
    at = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)
    return f"**Sent:** delivery not recorded — moved from the per-host file at {at.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def legacy_entries(text: str) -> list:
    """Every active-region entry: ask id (its own or a digest), title, body, whether it is
    open or already moved, its settled **Sent:** line (None while still being asked) and the
    structured fields when an older head left them."""
    out = []
    for s, e in _sections(text):
        sec = text[s:e]
        ids, st = ASK_ID_LINE_RE.findall(sec), _STATUS_LINE_RE.search(sec)
        if len(ids) > 1:
            continue
        head, _, body = sec[3:].partition("\n")
        title = _HEADING_TS_RE.sub("", head.strip())
        if not title or _ORG_HEADING.match(title) or _INLINE_RESOLVED.match(title):
            continue
        word = st.group(0)[len("**Status:**"):].strip().lower() if st else ""
        sent = _SENT_RE.search(sec)
        if sent and _PLACEHOLDER_RE.match(sent.group(0)):
            settled = None  # a live run of an older head is still asking it
        else:
            settled = sent.group(0) if sent else (None if ids else legacy_sent_line())
        ask_id = ids[0] if ids else digest_key(sec)
        out.append({"ask_id": ask_id, "title": title, "body": body.strip(), "span": (s, e), "kind": "section",
                    "open": not word or word.startswith(OPEN_WORDS), "moved": word.startswith(MOVED),
                    "sent": settled, "question": _question_from_fields(ids[0], sec) if ids else None})
    for s, e in _bullets(text):
        line = text[s:e]
        m, st = _BULLET_RE.match(line), _STATUS_ANY_RE.search(line)
        word = st.group(0)[len("**Status:**"):].strip().lower() if st else ""
        out.append({"ask_id": digest_key(line), "title": m.group(1).strip(), "body": line.strip(), "span": (s, e),
                    "kind": "bullet", "open": not word or word.startswith(OPEN_WORDS),
                    "moved": word.startswith(MOVED), "sent": legacy_sent_line(), "question": None})
    return out


def _mark_moved(path: Path, ask_id: str, row: str) -> Optional[str]:
    """Rewrite the entry's Status line as moved; a section without one gets it after its
    heading, a bullet at its end. Located again in the current text, by the same key."""
    mark = f"**Status:** {MOVED} — as row {row}"

    def _do(old: str) -> str:
        hits = [e for e in legacy_entries(old) if e["ask_id"] == ask_id]
        if len(hits) != 1:
            raise ledger.LedgerError(f"ask id {ask_id!r} names {len(hits)} entries, expected 1")
        s, e = hits[0]["span"]
        if hits[0]["kind"] == "bullet":
            return old[:e] + " " + mark + old[e:]
        m = _STATUS_LINE_RE.search(old, s, e)
        if m:
            return old[:m.start()] + mark + old[m.end():]
        nl = old.index("\n", s) if "\n" in old[s:e] else e
        return old[:nl] + "\n\n" + mark + old[nl:]
    return ledger.update(path, _do)


def ingest_legacy_file_entries(workspace, host: Optional[str], store) -> tuple:
    """(ask ids moved, errors). Open, settled entries with no complete row of this host get
    one (an incomplete row is resumed); the file entry is marked only once the row is
    confirmed complete. Nothing is ever retired or re-opened."""
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
        have = {r["ask_id"] for r in store.entries() if not r["incomplete"]}
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
            mark_store_used(workspace, aid)  # the durable fact first; the file entry stays open if it fails
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
