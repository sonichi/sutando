#!/usr/bin/env python3
"""One writer for task-correlation records: <workspace>/state/correlation.jsonl.

Every record is one JSON line with a `kind`; this module owns the schema of each kind, the field
bounds, the locked append, rotation, and the failure semantics, and every writer calls it. A
record names exact ids only; nothing here infers a link.

    kind "outcome_signal": evidence about how a task's result landed, attached beside its label and
    never replacing it; for now a reaction on the agent's own result message
        {kind, at, signal, value, reactor_id, reactor_relation, evidence, ref, task_id?, room_id?}

    kind "interaction": an ask we made, so the answer can be joined back by its id, with where it
    happened, captured when it happens and by id only (no names)
        {kind, at, interaction_id, object, counterpart, counterpart_kind?, counterpart_scope?,
         room_id?, room_kind?, task_id?, run_id?, channel?, event_id?}

Identity fields are checked per field: Matrix ids for counterpart/room/event, `owner/repo#N` for
object, the engine's task and run id shapes; a display name or prose never lands in a record or a
trailer. `record()` never raises, whatever it is given: correlation is bookkeeping beside the work,
so a failed or partial write returns None (said once on stderr) and leaves the file as it was.
`trailers()` renders the git trailers our commits and PRs carry.

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

try:  # relative-first, so the ag2-sparrow package's synced copy uses its own siblings
    from .file_lock import locked_file
    from .local_task_protocol import valid_archive_lookup_id
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from file_lock import locked_file  # noqa: E402
    from local_task_protocol import valid_archive_lookup_id  # noqa: E402

LOG_NAME = "correlation.jsonl"
MAX_BYTES = 8 * 1024 * 1024
FIELD_MAX = 300
INTERACTION_RE = re.compile(r"^ix_[0-9a-f]{12}$")
_HOST = r"[A-Za-z0-9.-]+(?::\d{1,5})?"


# The producer families task files are named by, after an optional `task-<lane>~` prefix (lane keys
# such as s_dspace); a Discord task is `task-dc<channel>~<message id>`. Nothing else is a task id.
_SEG = r"[a-z0-9_-]{1,120}"
_TASK_ID = re.compile(rf"^(?:(?:staging-)?task-{_SEG}~)?"
                      rf"(?:(?:task|worker-pin|worker-add|ask|sc-ask|reco-skill|voice-end|air)-{_SEG}|\d{{6,24}})$")


def is_task_id(value: str) -> bool:
    """A task id as the engine's producers mint them, in the shared filename-safe charset."""
    return isinstance(value, str) and valid_archive_lookup_id(value) and bool(_TASK_ID.match(value))


def is_run_id(value: str) -> bool:
    """A task id plus `~<attempt>`."""
    task, _, n = value.rpartition("~") if isinstance(value, str) else ("", "", "")
    return n.isdigit() and len(n) <= 9 and is_task_id(task)


PATTERNS = {
    "counterpart": re.compile(rf"^@[a-z0-9._=/+-]{{1,255}}:{_HOST}$"),
    "room_id": re.compile(rf"^![A-Za-z0-9._~=+-]{{1,255}}:{_HOST}$"),
    "event_id": re.compile(rf"^\$[A-Za-z0-9_+/=-]{{1,255}}(?::{_HOST})?$"),
    "object": re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}#\d{1,9}$"),
    "interaction_id": INTERACTION_RE,
}
PATTERNS["reactor_id"] = PATTERNS["counterpart"]
PATTERNS["ref"] = PATTERNS["event_id"]
# A reaction key as Matrix carries it: an emoji or a short token, one line, no spaces.
PATTERNS["value"] = re.compile(r"^\S{1,16}$")
CHECKS = {"task_id": is_task_id, "run_id": is_run_id}

KINDS = {
    "outcome_signal": {"required": ("signal", "value", "reactor_id", "reactor_relation", "evidence", "ref"),
                       "optional": ("task_id", "room_id")},
    "interaction": {"required": ("interaction_id", "object", "counterpart"),
                    "optional": ("counterpart_kind", "counterpart_scope", "room_id", "room_kind",
                                 "task_id", "run_id", "channel", "event_id")},
}
ENUMS = {"counterpart_kind": ("person", "agent", "owner"), "counterpart_scope": ("internal", "external", "unknown"),
         "room_kind": ("dm", "room", "unknown"), "channel": ("room", "dm", "thread"),
         "signal": ("reaction", "reaction_removed"), "reactor_relation": ("owner", "member"),
         "evidence": ("direct", "correlated", "weak", "inferred")}

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
    spec = KINDS.get(kind) if isinstance(kind, str) else None
    if spec is None:
        raise ValueError(f"unknown kind {kind!r}")
    if not isinstance(fields, dict):
        raise ValueError(f"fields must be a dict, not {type(fields).__name__}")
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
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if len(value) > FIELD_MAX or "\n" in value:
            raise ValueError(f"{key} is too long or spans lines")
        rec[key] = value
    for key, allowed in ENUMS.items():
        if key in rec and rec[key] not in allowed:
            raise ValueError(f"{key} must be one of {allowed}")
    for key, ok in list(PATTERNS.items()) + list(CHECKS.items()):
        if key in rec and not (ok.match(rec[key]) if hasattr(ok, "match") else ok(rec[key])):
            raise ValueError(f"{key} is not a valid id for that field")
    return rec


def _append(path: Path, line: bytes) -> None:
    """The whole line or nothing: a short write is retried, a failed one is truncated back."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        start = os.fstat(fd).st_size
        if start and os.pread(fd, 1, start - 1) != b"\n":
            line = b"\n" + line  # a crash left a partial line: never merge the next record into it
        done = 0
        try:
            while done < len(line):
                n = os.write(fd, line[done:])
                if n <= 0:
                    raise OSError("write made no progress")
                done += n
            os.fsync(fd)
        except BaseException:
            os.ftruncate(fd, start)
            raise
    finally:
        os.close(fd)


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
            _append(path, line)
        return rec
    except Exception as e:  # noqa: BLE001 - the never-raise boundary, whatever the input or store
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
    for label, value, ok in (("Sutando-Task", task_id, is_task_id), ("Sutando-Run", run_id, is_run_id),
                             ("Interaction-Id", interaction_id, INTERACTION_RE.match)):
        if value:
            value = str(value).strip()
            if not ok(value):
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
