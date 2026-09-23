#!/usr/bin/env python3
"""gdocs-write-guard — a whole-document replace on a Google Doc needs a fresh
read-back first; every read is kept as a restorable snapshot.

Why (user feedback, 2026-09-20): "when I ask Sutando to edit a Google Doc, the
document sometimes gets unexpectedly cleared, or unrelated content is inserted
or rewritten." The Station's Google Docs connector exposes
``GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN``, whose contract is "replaces the entire
content of an existing document" — the natural tool for a model that wants to
"update the doc", and the one that wipes everything the owner had in it when
the model rewrites from a partial memory. Nothing warned the model, and nothing
kept a copy to restore from.

Two hook events, one script (registered by build-core-settings.mjs on the
``mcp__sutando-station__composio_exec`` tool, toolkit ``googledocs``):

  PreToolUse   — a body-replacing action (UPDATE_DOCUMENT_MARKDOWN,
                 UPDATE_EXISTING_DOCUMENT, REPLACE_DOCUMENT, DELETE_CONTENT_RANGE)
                 is DENIED unless a snapshot of that document younger than
                 ``SUTANDO_GDOCS_BACKUP_MAX_AGE_S`` (default 900 s) exists. The
                 reason tells the model to read the document first and to
                 prefer the partial-edit actions (INSERT_TEXT_ACTION,
                 REPLACE_ALL_TEXT, INSERT_TEXT_IN_TABLE_CELL), which are always
                 allowed.
  PostToolUse  — a read (GET_DOCUMENT_PLAINTEXT, GET_DOCUMENT_BY_ID, any
                 GET_DOCUMENT*) writes ``<workspace>/data/gdocs-backups/<doc
                 id>/<epoch>.md`` (atomic tmp+rename, newest 20 kept), so a
                 wrong rewrite can be undone from the last thing the owner had.

Scope: only ``composio_exec`` calls whose ``toolkit`` is googledocs; every
other tool, toolkit and action is a no-op (exit 0, no output), so it is safe
under the broad ``composio_exec`` matcher. Escape hatch:
``SUTANDO_ALLOW_GDOCS_WHOLE_REPLACE=1`` (an intentional full rewrite the owner
asked for, when the snapshot dance is in the way).

Fail-OPEN on any error — a crashing hook must never wedge the core (same
contract as gmail-write-guard.py). Test: tests/gdocs-write-guard.test.py.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

EXEC_TOOL_SUFFIX = "__composio_exec"
TOOLKIT = "googledocs"
# Substrings of the (upper-cased) action name. A whole-replace rewrites or removes
# the body; a read is what the snapshot comes from. Partial edits are neither.
WHOLE_REPLACE = ("UPDATE_DOCUMENT_MARKDOWN", "UPDATE_EXISTING_DOCUMENT", "REPLACE_DOCUMENT", "DELETE_CONTENT_RANGE")
READS = ("GET_DOCUMENT",)
PARTIAL_EDITS = "GOOGLEDOCS_INSERT_TEXT_ACTION, GOOGLEDOCS_REPLACE_ALL_TEXT, GOOGLEDOCS_INSERT_TEXT_IN_TABLE_CELL"
BACKUP_DIR = ("data", "gdocs-backups")
KEEP = 20
DEFAULT_MAX_AGE_S = 900.0
DOC_ID_KEYS = ("document_id", "documentId", "id", "file_id", "fileId", "documentid")
DOC_URL = re.compile(r"/document/d/([A-Za-z0-9_-]+)")
SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


def is_exec_tool(tool_name: str) -> bool:
    return tool_name.startswith("mcp__") and tool_name.endswith(EXEC_TOOL_SUFFIX)


def doc_id_of(arguments) -> str | None:
    """The document id from the action's arguments: a bare id or a docs.google.com URL."""
    if not isinstance(arguments, dict):
        return None
    for key in DOC_ID_KEYS:
        raw = arguments.get(key)
        if isinstance(raw, str) and raw.strip():
            m = DOC_URL.search(raw)
            value = m.group(1) if m else raw.strip()
            return SAFE_ID.sub("_", value)[:200] or None
    return None


def classify(action: str) -> str:
    """whole-replace | read | other, from the action name alone."""
    up = (action or "").upper()
    if any(tok in up for tok in WHOLE_REPLACE):
        return "whole-replace"
    if any(tok in up for tok in READS):
        return "read"
    return "other"


def workspace() -> Path | None:
    env = os.environ.get("SUTANDO_WORKSPACE_DIR")
    if env:
        return Path(env).expanduser()
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from workspace_default import resolve_workspace  # noqa: PLC0415
        return Path(resolve_workspace())
    except Exception:  # noqa: BLE001 — no workspace, no verdict
        return None


def backup_dir(ws: Path, doc_id: str) -> Path:
    return ws.joinpath(*BACKUP_DIR) / doc_id


def fresh_backup(ws: Path, doc_id: str, now: float, max_age: float) -> Path | None:
    """The newest snapshot of the document younger than max_age, else None."""
    d = backup_dir(ws, doc_id)
    try:
        files = [p for p in d.iterdir() if p.is_file() and p.suffix == ".md"]
    except OSError:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime, default=None)
    if newest is None:
        return None
    return newest if 0 <= now - newest.stat().st_mtime <= max_age else None


def response_text(tool_response) -> str:
    """The readable body of a hook's tool_response: a string, a list of content blocks, or JSON."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, list):
        parts = []
        for block in tool_response:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            return "\n".join(parts)
    if isinstance(tool_response, dict):
        inner = tool_response.get("content")
        if inner is not None and inner is not tool_response:
            got = response_text(inner)
            if got:
                return got
        for key in ("plain_text", "text"):
            if isinstance(tool_response.get(key), str):
                return tool_response[key]
    try:
        return json.dumps(tool_response, ensure_ascii=False, indent=1)
    except (TypeError, ValueError):
        return str(tool_response)


def write_backup(ws: Path, doc_id: str, text: str, now: float) -> Path:
    d = backup_dir(ws, doc_id)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{int(now * 1000)}.md"
    tmp = d / f".{out.name}.{os.getpid()}.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, out)
    try:
        files = sorted((p for p in d.iterdir() if p.is_file() and p.suffix == ".md"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[KEEP:]:
            old.unlink()
    except OSError:
        pass
    return out


def deny_reason(doc_id: str | None, ws: Path | None) -> str:
    where = f"{Path(*BACKUP_DIR)}/<doc id>/" if ws is None else str(backup_dir(ws, doc_id or "<doc id>"))
    return (
        f"Whole-document replace on Google Doc {doc_id or '(id not named in the arguments)'} is blocked: "
        f"no read-back from the last {int(DEFAULT_MAX_AGE_S // 60)} minutes exists to restore from, and this "
        "action rewrites the ENTIRE document (owner report 2026-09-20: a doc was cleared and rewritten). "
        "First read it — GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT (or GET_DOCUMENT_BY_ID) with the document_id; the "
        f"PostToolUse hook keeps that read as a snapshot under {where}. Then prefer the partial-edit actions for "
        f"an edit: {PARTIAL_EDITS}. Run the full replace only for a rewrite the owner explicitly asked for, "
        "after the read. Set SUTANDO_ALLOW_GDOCS_WHOLE_REPLACE=1 to lift this guard. [gdocs-write-guard]"
    )


def handle(payload: dict, now: float | None = None) -> dict | None:
    """The hook's stdout JSON for this payload, or None for silence."""
    now = time.time() if now is None else now
    tool_name = str(payload.get("tool_name") or "")
    inp = payload.get("tool_input")
    if not is_exec_tool(tool_name) or not isinstance(inp, dict):
        return None
    if str(inp.get("toolkit") or "").strip().lower() != TOOLKIT:
        return None
    kind = classify(str(inp.get("action") or ""))
    event = payload.get("hook_event_name")
    doc_id = doc_id_of(inp.get("arguments"))
    ws = workspace()
    if event == "PostToolUse":
        if kind == "read" and doc_id and ws is not None:
            text = response_text(payload.get("tool_response"))
            if text.strip():
                write_backup(ws, doc_id, text, now)
        return None
    if event != "PreToolUse" or kind != "whole-replace":
        return None
    if os.environ.get("SUTANDO_ALLOW_GDOCS_WHOLE_REPLACE", "").strip() == "1":
        return None
    try:
        max_age = float(os.environ.get("SUTANDO_GDOCS_BACKUP_MAX_AGE_S") or DEFAULT_MAX_AGE_S)
    except ValueError:
        max_age = DEFAULT_MAX_AGE_S
    if doc_id and ws is not None and fresh_backup(ws, doc_id, now, max_age):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": deny_reason(doc_id, ws)}}


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    out = handle(payload) if isinstance(payload, dict) else None
    if out:
        print(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # fail-open: never wedge the core on a hook error
        print(f"[gdocs-write-guard] non-fatal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
