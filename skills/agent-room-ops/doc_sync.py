#!/usr/bin/env python3
"""room-ops · doc_sync — get/put a shared Room Context doc without last-writer-wins.

A shared record doc (rows keyed `owner/repo#N | ...` under `## Section` headers) has
many writers; a full-content `doc put` from a stale read silently erases every edit
made since that read. `doc_sync` keeps a base copy per (folder, name) from the last
`get`, and `put` applies the caller's ROW-LEVEL changes (add, edit, move, retire)
onto the CURRENT remote, refusing only when the same row changed on both sides.
Structure lines (headers, prose) are never merged: a change there refuses.

Every row the caller adds or edits is stamped `(w:<writer>)` from SUTANDO_CORE_ID
(or --writer); an unset id stamps `unknown`, never a shape-valid empty slot.

    doc_sync.py get --room R --name N [--folder F] [--workspace W]
    doc_sync.py put --room R --name N --file EDITED [--folder F] [--workspace W] [--writer ID]

Room/name come from flags or ROOM_DOC_ROOM / ROOM_DOC_NAME / ROOM_DOC_FOLDER; a missing
required value fails naming it (exit 2). Exit: 0 ok · 1 transport · 2 config · 3 not found
after one retry · 4 refused (no base, conflict, structure change) · 5 put not verified.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

RECORD_RE = re.compile(r"^([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+#\d+)\s*\|")
SECTION_RE = re.compile(r"^## (.+?)\s*$")
STAMP_RE = re.compile(r"\s*\(w:[^)]*\)\s*$")
RETRY_AFTER_S = 3.0


class Record(NamedTuple):
    section: str
    text: str


def parse(text: str) -> Tuple[List[str], Dict[str, Record]]:
    """Structure lines (everything that is not a record) and records keyed by their id."""
    structure, records, section = [], {}, ""
    for line in text.split("\n"):
        m = SECTION_RE.match(line)
        if m:
            section = m.group(1)
        k = RECORD_RE.match(line)
        if k:
            records.setdefault(k.group(1), Record(section, line))
        else:
            structure.append(line)
    return structure, records


def writer_id(explicit: Optional[str] = None, env=None) -> str:
    env = os.environ if env is None else env
    w = (explicit or env.get("SUTANDO_CORE_ID") or "").strip()
    return w or "unknown"


def stamp(line: str, writer: str) -> str:
    return f"{STAMP_RE.sub('', line).rstrip()} (w:{writer})"


def _stamp_of(line: str) -> str:
    m = re.search(r"\(w:([^)]*)\)\s*$", line)
    return m.group(1) if m else "unstamped"


def _insert_into_section(lines: List[str], section: str, text: str) -> bool:
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if m and m.group(1) == section:
            lines.insert(i + 1, text)
            return True
    return False


def duplicates(text: str) -> Dict[str, int]:
    """Keys that occur more than once (a row present in two sections has no single identity)."""
    counts: Dict[str, int] = {}
    for line in text.split("\n"):
        k = RECORD_RE.match(line)
        if k:
            counts[k.group(1)] = counts.get(k.group(1), 0) + 1
    return {k: n for k, n in counts.items() if n > 1}


def merge(base: str, mine: str, remote: str, writer: str) -> Tuple[str, List[str], List[str]]:
    """Apply my row deltas (base->mine) onto remote. Returns (text, applied, conflicts);
    a non-empty conflicts list means nothing should be written."""
    sb, rb = parse(base)
    sm, rm = parse(mine)
    if sb != sm:
        return remote, [], ["structure lines changed (headers/prose); edit records only"]
    _, rr = parse(remote)
    dup = duplicates(remote)
    lines = remote.split("\n")
    applied, conflicts = [], []

    def find(key: str) -> int:
        for i, line in enumerate(lines):
            k = RECORD_RE.match(line)
            if k and k.group(1) == key:
                return i
        return -1

    for key in sorted(set(rb) | set(rm)):
        b, m, r = rb.get(key), rm.get(key), rr.get(key)
        if b == m:
            continue
        if key in dup:
            conflicts.append(f"{key}: appears {dup[key]} times remotely; resolve the duplicate by hand first")
            continue
        if m is None:                                   # I retired the row
            if r is None:
                continue
            if r != b:
                conflicts.append(f"{key}: retired by me, changed remotely (w:{_stamp_of(r.text)})")
                continue
            del lines[find(key)]
            applied.append(f"retire {key}")
            continue
        new = Record(m.section, stamp(m.text, writer))
        if b is None:                                   # I added the row
            if r is not None:
                if r.text == m.text or r.text == new.text:
                    continue
                conflicts.append(f"{key}: added by me, already present remotely (w:{_stamp_of(r.text)})")
                continue
            if not _insert_into_section(lines, new.section, new.text):
                conflicts.append(f"{key}: section '## {new.section}' not found remotely")
                continue
            applied.append(f"add {key} -> {new.section}")
            continue
        if r is None:
            conflicts.append(f"{key}: edited by me, removed remotely")
            continue
        if r.text == m.text or r.text == new.text:
            continue
        if r != b:
            conflicts.append(f"{key}: edited by me AND remotely (w:{_stamp_of(r.text)})")
            continue
        i = find(key)
        if r.section == new.section:
            lines[i] = new.text
            applied.append(f"edit {key}")
        else:
            del lines[i]
            if not _insert_into_section(lines, new.section, new.text):
                conflicts.append(f"{key}: section '## {new.section}' not found remotely")
                continue
            applied.append(f"move {key} {r.section} -> {new.section}")
    return "\n".join(lines), applied, conflicts


# -- transport seams (tests replace these) ---------------------------------------------

def run_get(room: str, folder: str, name: str) -> dict:
    import doc  # noqa: WPS433 - sibling module, same skill dir
    return doc.doc_get(room, folder=folder, name=name)


def run_put(room: str, folder: str, name: str, content: str) -> dict:
    import doc  # noqa: WPS433
    return doc.doc_put(room, content, folder=folder, name=name)


def fetch(room: str, folder: str, name: str, sleep=time.sleep) -> Tuple[int, dict]:
    # A transient failure has been observed to render as "not found"; one retry tells them apart.
    res = run_get(room, folder, name)
    if not res.get("ok") and "not found" in str(res.get("reason") or ""):
        sleep(RETRY_AFTER_S)
        res = run_get(room, folder, name)
        if not res.get("ok") and "not found" in str(res.get("reason") or ""):
            return 3, res
    if not res.get("ok"):
        return 1, res
    if not isinstance(res.get("content"), str):
        return 1, {"ok": False, "reason": "ok=true but no content string"}
    return 0, res


def base_path(workspace: Path, folder: str, name: str) -> Path:
    return Path(workspace) / "state" / "room-doc-sync" / f"{folder}__{name}.base"


def cmd_get(room: str, folder: str, name: str, workspace: Path) -> int:
    rc, res = fetch(room, folder, name)
    if rc:
        print(f"get FAILED rc={rc}: {res.get('reason')}", file=sys.stderr)
        return rc
    p = base_path(workspace, folder, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(res["content"])
    print(f"ok  {folder}/{name}  {len(res['content'])} chars  base -> {p}")
    return 0


def cmd_put(room: str, folder: str, name: str, workspace: Path, edited: Path, writer: str) -> int:
    p = base_path(workspace, folder, name)
    if not p.exists():
        print("put REFUSED (4): no base copy for this doc — run `get`, edit that, then put", file=sys.stderr)
        return 4
    rc, res = fetch(room, folder, name)
    if rc:
        print(f"put REFUSED: pre-put read failed rc={rc}: {res.get('reason')}", file=sys.stderr)
        return rc
    merged, applied, conflicts = merge(p.read_text(), edited.read_text(), res["content"], writer)
    if conflicts:
        print("put REFUSED (4): " + "; ".join(conflicts) + " — re-run `get`, re-apply, put", file=sys.stderr)
        return 4
    if merged == res["content"]:
        print("put: no row changes to apply; nothing written")
        p.write_text(merged)
        return 0
    put = run_put(room, folder, name, merged)
    if not put.get("ok"):
        print(f"put FAILED: {put.get('reason')}", file=sys.stderr)
        return 1
    rc, back = fetch(room, folder, name)
    if rc or back["content"] != merged:
        print("put NOT VERIFIED (5): re-get " + (f"failed: {back.get('reason')}" if rc else "differs from what was put"),
              file=sys.stderr)
        return 5
    p.write_text(merged)
    print(f"ok  put + verified  applied: {', '.join(applied)}  (w:{writer})")
    return 0


def _required(value: Optional[str], flag: str, env_key: str) -> str:
    if value:
        return value
    print(f"config error (2): {flag} not given and {env_key} unset", file=sys.stderr)
    raise SystemExit(2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["get", "put"])
    ap.add_argument("--room", default=os.environ.get("ROOM_DOC_ROOM"))
    ap.add_argument("--folder", default=os.environ.get("ROOM_DOC_FOLDER") or "room-live-context")
    ap.add_argument("--name", default=os.environ.get("ROOM_DOC_NAME"))
    ap.add_argument("--file", help="put: the edited document")
    ap.add_argument("--workspace", help="workspace root (default: the repo's resolver)")
    ap.add_argument("--writer", help="row stamp (default: SUTANDO_CORE_ID, else 'unknown')")
    a = ap.parse_args(argv)
    room = _required(a.room, "--room", "ROOM_DOC_ROOM")
    name = _required(a.name, "--name", "ROOM_DOC_NAME")
    if a.workspace:
        ws = Path(a.workspace)
    else:
        sys.path.insert(0, str(HERE.parent.parent / "src"))
        from workspace_default import resolve_workspace  # noqa: WPS433
        ws = Path(resolve_workspace())
    if a.action == "get":
        return cmd_get(room, a.folder, name, ws)
    if not a.file:
        ap.error("put needs --file")
    return cmd_put(room, a.folder, name, ws, Path(a.file), writer_id(a.writer))


if __name__ == "__main__":
    sys.exit(main())
