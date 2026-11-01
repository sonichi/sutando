#!/usr/bin/env python3
"""One writer for task-correlation records: <workspace>/state/correlation.jsonl.

Every record is one JSON line with a `kind`; this module owns the schema of each kind, the field
bounds, the locked append, rotation, and the failure semantics, and every writer calls it. A
record names exact ids only; nothing here infers a link.

    kind "interaction": an ask we made, so the answer can be joined back by its id, with where it
    happened, captured when it happens and by id only (no names)
        {kind, at, interaction_id, object, counterpart, counterpart_kind?, counterpart_scope?,
         room_id?, room_kind?, task_id?, run_id?, channel?, event_id?}

`record()` never raises: correlation is bookkeeping beside the work, so a failed write returns
None and says so once on stderr. `trailers()` renders the git trailers our commits and PRs carry.

    python3 src/correlation_log.py mint
    python3 src/correlation_log.py trailers --task <task id> [--run <run id>] [--interaction <id>]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from file_lock import locked_file  # noqa: E402

LOG_NAME = "correlation.jsonl"
MAX_BYTES = 8 * 1024 * 1024
FIELD_MAX = 300
INTERACTION_RE = re.compile(r"^ix_[0-9a-f]{12}$")
ID_RE = re.compile(r"^[A-Za-z0-9._:~#/@!$+=-]{1,200}$")

KINDS = {
    "interaction": {"required": ("interaction_id", "object", "counterpart"),
                    "optional": ("counterpart_kind", "counterpart_scope", "room_id", "room_kind",
                                 "task_id", "run_id", "channel", "event_id")},
}
ENUMS = {"counterpart_kind": ("person", "agent", "owner"), "counterpart_scope": ("internal", "external", "unknown"),
         "room_kind": ("dm", "room", "unknown")}

_warned = False


def log_path(workspace: "str | Path | None" = None) -> Path:
    if workspace is None:
        from workspace_default import resolve_workspace
        workspace = resolve_workspace()
    return Path(workspace) / "state" / LOG_NAME


def mint_interaction_id() -> str:
    return "ix_" + secrets.token_hex(6)


def validate(kind: str, fields: dict) -> dict:
    """The record as it will be written, or ValueError naming what is wrong."""
    spec = KINDS.get(kind)
    if spec is None:
        raise ValueError(f"unknown kind {kind!r}")
    unknown = set(fields) - set(spec["required"]) - set(spec["optional"])
    if unknown:
        raise ValueError(f"unknown fields for {kind}: {sorted(unknown)}")
    rec = {"kind": kind, "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    for key in spec["required"] + spec["optional"]:
        value = fields.get(key)
        if value is None or value == "":
            if key in spec["required"]:
                raise ValueError(f"{kind} needs {key}")
            continue
        value = str(value)
        if len(value) > FIELD_MAX or "\n" in value:
            raise ValueError(f"{key} is too long or spans lines")
        rec[key] = value
    for key, allowed in ENUMS.items():
        if key in rec and rec[key] not in allowed:
            raise ValueError(f"{key} must be one of {allowed}")
    if kind == "interaction" and not INTERACTION_RE.match(rec["interaction_id"]):
        raise ValueError("interaction_id is not ix_<12 hex>")
    for key in ("task_id", "run_id"):
        if key in rec and not ID_RE.match(rec[key]):
            raise ValueError(f"{key} is not an id")
    return rec


def record(kind: str, fields: dict, *, workspace=None, path: "Path | None" = None) -> "dict | None":
    """Append one record; the record written, or None (said once on stderr) if it could not be."""
    global _warned
    try:
        rec = validate(kind, fields)
        path = Path(path) if path is not None else log_path(workspace)
        line = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
        with locked_file(path.with_name(LOG_NAME + ".lock")):
            if path.exists() and path.stat().st_size + len(line) > MAX_BYTES:
                os.replace(path, path.with_name(LOG_NAME + ".1"))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
        return rec
    except (OSError, ValueError) as e:
        if not _warned:
            _warned = True
            print(f"correlation_log: not recorded ({type(e).__name__}: {e})", file=sys.stderr)
        return None


def read(kind: "str | None" = None, *, workspace=None, path: "Path | None" = None) -> list:
    """Every record of `kind` (or all), oldest first, across the rotated file and the live one."""
    path = Path(path) if path is not None else log_path(workspace)
    out = []
    for p in (path.with_name(LOG_NAME + ".1"), path):
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and (kind is None or rec.get("kind") == kind):
                out.append(rec)
    return out


def trailers(task_id: "str | None" = None, run_id: "str | None" = None,
             interaction_id: "str | None" = None) -> str:
    """Git trailer lines for a commit or PR body; empty when no id is given."""
    lines = []
    for label, value in (("Sutando-Task", task_id), ("Sutando-Run", run_id), ("Interaction-Id", interaction_id)):
        if value:
            value = str(value).strip()
            if not (INTERACTION_RE if label == "Interaction-Id" else ID_RE).match(value):
                raise ValueError(f"{label}: {value!r} is not an id")
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Task-correlation ids and trailers.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mint", help="print a new interaction id")
    t = sub.add_parser("trailers", help="print git trailers for a commit or PR body")
    t.add_argument("--task")
    t.add_argument("--run")
    t.add_argument("--interaction")
    a = ap.parse_args(argv)
    if a.cmd == "mint":
        print(mint_interaction_id())
        return 0
    try:
        print(trailers(a.task, a.run, a.interaction))
    except ValueError as e:
        print(f"correlation_log: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
