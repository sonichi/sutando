#!/usr/bin/env python3
"""gdocs-write-guard — a whole-document replace on a Google Doc is denied until the
document was read back recently; every successful read is kept as a restorable snapshot.

One script on two hook events for the Station's ``composio_exec`` tool, toolkit
``googledocs`` (registered by build-core-settings.mjs):

  PreToolUse   — a body-replacing action (UPDATE_DOCUMENT_MARKDOWN, UPDATE_EXISTING_DOCUMENT,
                 REPLACE_DOCUMENT, DELETE_CONTENT_RANGE) is DENIED unless a snapshot of that
                 document younger than the max age (900 s by default) exists. The reason says
                 to read the document first and to prefer the partial-edit actions
                 (INSERT_TEXT_ACTION, REPLACE_ALL_TEXT, INSERT_TEXT_IN_TABLE_CELL), which are
                 always allowed.
  PostToolUse  — a SUCCESSFUL read (GET_DOCUMENT_PLAINTEXT, GET_DOCUMENT_BY_ID, any
                 GET_DOCUMENT*) is written to ``<workspace>/data/gdocs-backups/<doc
                 id>/<epoch>.md`` (atomic tmp+rename, newest 20 kept). A failed, errored or
                 empty read is never a snapshot: it could restore nothing, so it must not
                 lift the deny.

Settings, read through ``sutando_config`` (the ``env`` stanza of
``sutando.config.local.json``, the environment as the fallback):
  SUTANDO_GDOCS_BACKUP_MAX_AGE_S     how old a snapshot may be and still vouch (default 900).
  SUTANDO_ALLOW_GDOCS_WHOLE_REPLACE  ``1`` lifts the guard — an operator override; the
                                     in-session way past the deny is the read itself.

The repo root is configured, never discovered: ``--repo <path>`` in the registration or
``$SUTANDO_REPO_ROOT``. The workspace comes from ``workspace_default.resolve_workspace``.
Without a root no snapshot can be recorded, so a whole replace stays denied and stderr says
why. Every other tool, toolkit and action is a no-op (exit 0, no output). Fail-OPEN on any
uncaught error, so a crashing hook never wedges the core. Test: tests/gdocs-write-guard.test.py.
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
MAX_AGE_KEY = "SUTANDO_GDOCS_BACKUP_MAX_AGE_S"
ALLOW_KEY = "SUTANDO_ALLOW_GDOCS_WHOLE_REPLACE"
DOC_ID_KEYS = ("document_id", "documentId", "id", "file_id", "fileId", "documentid")
DOC_URL = re.compile(r"/document/d/([A-Za-z0-9_-]+)")
SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")
# How a connector envelope says the call failed, and where it carries the document text.
FAILURE_FLAGS = ("successful", "success")
ERROR_KEYS = ("error", "is_error", "isError")
BODY_KEYS = ("plain_text", "plainText", "text", "markdown", "content", "body")


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


def repo_root(argv) -> str | None:
    """Configured, never discovered: ``--repo <path>`` / ``--repo=<path>``, else $SUTANDO_REPO_ROOT."""
    for i, a in enumerate(argv):
        if a == "--repo" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--repo="):
            return a.split("=", 1)[1]
    return os.environ.get("SUTANDO_REPO_ROOT") or None


def _src_on_path(root: str) -> None:
    src = os.path.join(root, "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def workspace(root: str | None) -> Path | None:
    """The workspace through the repo helper; None (with a stderr note) when it cannot be resolved."""
    if not root:
        print("[gdocs-write-guard] repo root not configured (--repo or SUTANDO_REPO_ROOT): no snapshots",
              file=sys.stderr)
        return None
    try:
        _src_on_path(root)
        from workspace_default import resolve_workspace  # noqa: PLC0415
        return Path(resolve_workspace())
    except Exception as e:  # noqa: BLE001
        print(f"[gdocs-write-guard] workspace unresolved, no snapshots: {e}", file=sys.stderr)
        return None


def setting(root: str | None, key: str, env_first: bool = False) -> str:
    """A hook setting via sutando_config (config file ``env`` stanza / env var); the bare env without a repo."""
    if root:
        try:
            _src_on_path(root)
            import sutando_config  # noqa: PLC0415
            read = sutando_config.config_get_env_first if env_first else sutando_config.config_get
            return str(read(key) or "")
        except Exception as e:  # noqa: BLE001
            print(f"[gdocs-write-guard] config unreadable, using the environment: {e}", file=sys.stderr)
    return os.environ.get(key, "")


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


def _failed(envelope: dict) -> bool:
    """A connector envelope that says the call failed: successful/success False, or an error set."""
    if any(envelope.get(k) is False for k in FAILURE_FLAGS):
        return True
    return any(bool(envelope.get(k)) for k in ERROR_KEYS)


def _body(envelope: dict) -> str | None:
    """The document text inside an envelope (top level or under ``data``), else None."""
    for holder in (envelope, envelope.get("data")):
        if isinstance(holder, dict):
            for key in BODY_KEYS:
                if isinstance(holder.get(key), str):
                    return holder[key]
    return None


def response_text(tool_response) -> str | None:
    """The readable body of a hook's tool_response; None when the response itself reports a failure."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, list):
        parts = []
        for block in tool_response:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts) if parts else None
    if isinstance(tool_response, dict):
        if _failed(tool_response):
            return None
        inner = tool_response.get("content")
        if inner is not None and inner is not tool_response:
            got = response_text(inner)
            if got:
                return got
        body = _body(tool_response)
        if body is not None:
            return body
        try:
            return json.dumps(tool_response, ensure_ascii=False, indent=1)
        except (TypeError, ValueError):
            return str(tool_response)
    return None if tool_response is None else str(tool_response)


def snapshot_text(tool_response) -> str | None:
    """The document text a read returned; None for a failed, errored or empty read (nothing to restore)."""
    text = response_text(tool_response)
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        if _failed(parsed):
            return None
        body = _body(parsed)
        if body is not None:
            text = body
        elif "data" in parsed and not parsed["data"]:
            return None
    return text if text.strip() else None


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


def _age_label(max_age: float) -> str:
    minutes = max_age / 60
    return f"{int(minutes)} minutes" if minutes >= 1 and minutes == int(minutes) else f"{int(max_age)} seconds"


def deny_reason(doc_id: str | None, ws: Path | None, max_age: float = DEFAULT_MAX_AGE_S) -> str:
    where = f"{Path(*BACKUP_DIR)}/<doc id>/" if ws is None else str(backup_dir(ws, doc_id or "<doc id>"))
    age = _age_label(max_age)
    return (
        f"Whole-document replace on Google Doc {doc_id or '(id not named in the arguments)'} is blocked: "
        f"no read-back from the last {age} exists to restore from, and this action rewrites the ENTIRE "
        "document. First read it — GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT (or GET_DOCUMENT_BY_ID) with the "
        f"document_id; a successful read is kept as a snapshot under {where} and lifts this block for {age}. "
        f"Then prefer the partial-edit actions for an edit: {PARTIAL_EDITS}. Run the full replace only for a "
        "rewrite the owner explicitly asked for, after the read. Operator override: "
        f"{ALLOW_KEY}=1 in the env stanza of sutando.config.local.json or the environment. [gdocs-write-guard]"
    )


def handle(payload: dict, now: float | None = None, argv: list[str] | None = None) -> dict | None:
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
    root = repo_root(sys.argv[1:] if argv is None else argv)
    if event == "PostToolUse":
        if kind != "read" or not doc_id:
            return None
        text = snapshot_text(payload.get("tool_response"))
        if text is None:
            return None
        ws = workspace(root)
        if ws is not None:
            write_backup(ws, doc_id, text, now)
        return None
    if event != "PreToolUse" or kind != "whole-replace":
        return None
    if setting(root, ALLOW_KEY, env_first=True).strip() == "1":
        return None
    try:
        max_age = float(setting(root, MAX_AGE_KEY) or DEFAULT_MAX_AGE_S)
    except ValueError:
        max_age = DEFAULT_MAX_AGE_S
    ws = workspace(root)
    if doc_id and ws is not None and fresh_backup(ws, doc_id, now, max_age):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": deny_reason(doc_id, ws, max_age)}}


def main(argv: list[str] | None = None) -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    out = handle(payload, argv=argv) if isinstance(payload, dict) else None
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
