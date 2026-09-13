#!/usr/bin/env python3
"""A worker's durable identity: which worker, which conversation, which run.

Three questions with three different answers, and collapsing any two of them
loses information the others cannot recover:

    worker_id           which worker is this?      kept for the worker's life
    runtime_session_id  which conversation?        carried over on resume
    incarnation_id      which RUN of this worker?  new each execution instance

The runtime already distinguishes the two session cases — `--resume <id>` keeps
the session id, `--fork-session` mints a new one from the same history — so
lineage records the RELATION between sessions, never a flat list of ancestors.
A list cannot say whether a session continued from another or is unrelated, and
inventing a parent for an independent session is a fabrication every later
reader inherits.

Measured on this workspace before writing this: a core's own done-flags resolved
to three different sessions, because every restart began a new one. A worker maps
to MANY sessions over its life, so none of this is derivable after the fact — it
is recorded at start or it is lost.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# helpers live in the core; repo root is parents[3] from this directory
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from workspace_default import resolve_workspace  # noqa: E402

RELATION_NEW = "new"
RELATION_RESUMED = "resumed"
RELATION_FORKED = "forked"
RELATIONS = (RELATION_NEW, RELATION_RESUMED, RELATION_FORKED)

END_REASONS = ("exited", "crashed", "killed", "superseded", "unknown")

# `[a-z0-9][a-z0-9-]{0,31}` is the recipient-id bound; uuid4().hex is exactly 32.
WORKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class IdentityError(Exception):
    """A record was asked for something its schema cannot express."""


def new_worker_id() -> str:
    """122 random bits. A counter cannot satisfy 'never reused' across hosts:
    two machines on a synced workspace compute the same next value."""
    return uuid.uuid4().hex


def new_incarnation_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def worker_dir(workspace, worker_id: str) -> Path:
    """`workspace=None` resolves through the one sanctioned helper — a second
    resolution path is how readers and writers end up in different trees."""
    if not WORKER_ID_RE.match(worker_id):
        raise IdentityError(f"worker id must match {WORKER_ID_RE.pattern!r}: {worker_id!r}")
    root = Path(workspace) if workspace is not None else resolve_workspace()
    return root / "state" / "workers" / worker_id


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, payload) -> None:
    """Atomic FOR THE READER: no reader ever sees a truncated record.

    The suffix carries a uuid, not just the pid: two writers in ONE process
    share a pid, and the loser of that race gets FileNotFoundError from replace.
    `_appending` already serialises writers, so this is a second guard — keep it;
    it is what protects a caller that writes without the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def _appending(path: Path):
    """Read-modify-write under an exclusive lock, so no row is lost.

    `_write` alone protects the reader; it does not serialise two writers, and a
    lost lineage row is unrecoverable — it is recorded at start or never.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    with open(lock, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def sessions_path(workspace, worker_id): return worker_dir(workspace, worker_id) / "sessions.json"
def incarnations_path(workspace, worker_id): return worker_dir(workspace, worker_id) / "incarnations.json"
def current_path(workspace, worker_id): return worker_dir(workspace, worker_id) / "current.json"


def sessions(workspace, worker_id) -> list:
    return _read(sessions_path(workspace, worker_id), {}).get("sessions", [])


def incarnations(workspace, worker_id) -> list:
    return _read(incarnations_path(workspace, worker_id), {}).get("incarnations", [])


def current(workspace, worker_id) -> dict:
    return _read(current_path(workspace, worker_id),
                 {"session_id": None, "incarnation_id": None})


def record_session(workspace, worker_id: str, session_id: str, *, runtime: str,
                   relation: str, parent_session_id=None, host: str = "",
                   cwd: str = "", transcript_path: str = "") -> dict:
    """Add one session to the lineage. Resuming records NOTHING here — the
    conversation is unchanged; only a new run begins."""
    if relation not in RELATIONS:
        raise IdentityError(f"relation must be one of {RELATIONS}: {relation!r}")
    if relation == RELATION_FORKED and not parent_session_id:
        raise IdentityError("a forked session must name the session it came from")
    if relation == RELATION_NEW and parent_session_id:
        raise IdentityError("a new session has no parent — inventing one falsifies the lineage")

    path = sessions_path(workspace, worker_id)
    # The whole read-check-append-write is one critical section: the idempotence
    # check above is only meaningful if nobody appends between it and the write.
    with _appending(path):
        rows = sessions(workspace, worker_id)
        if any(r["session_id"] == session_id for r in rows):
            return next(r for r in rows if r["session_id"] == session_id)
        row = {"session_id": session_id, "runtime": runtime, "relation": relation,
               "parent_session_id": parent_session_id,
               "transcript": {"host": host, "cwd": cwd, "path": transcript_path},
               "first_seen": _now()}
        rows.append(row)
        _write(path, {"sessions": rows})
    return row


def start_incarnation(workspace, worker_id: str, session_id: str,
                      incarnation_id=None, tmux_socket: str = "",
                      tmux_session: str = "") -> dict:
    """Begin one run. A watcher restart, a terminal reattach, or a delivered
    message is NOT a new run and must not call this.

    The tmux locator belongs to the RUN: a later run may land in a fresh tmux
    session while `worker_id` stays put. Record the socket and the session NAME
    — tmux's own `$28`/`%28` ids are server-assigned counters, reassigned when
    the server restarts, so they point at nothing after one.
    """
    if not any(r["session_id"] == session_id for r in sessions(workspace, worker_id)):
        raise IdentityError(f"session {session_id!r} is not in this worker's lineage")
    row = {"incarnation_id": incarnation_id or new_incarnation_id(),
           "session_id": session_id, "started_at": _now(),
           "tmux": {"socket": tmux_socket, "session_name": tmux_session},
           "ended_at": None, "end_reason": None}
    path = incarnations_path(workspace, worker_id)
    with _appending(path):
        rows = incarnations(workspace, worker_id)
        rows.append(row)
        _write(path, {"incarnations": rows})
    _write(current_path(workspace, worker_id),
           {"session_id": session_id, "incarnation_id": row["incarnation_id"]})
    return row


def end_incarnation(workspace, worker_id: str, incarnation_id: str,
                    end_reason: str = "unknown") -> dict:
    if end_reason not in END_REASONS:
        raise IdentityError(f"end_reason must be one of {END_REASONS}: {end_reason!r}")
    path = incarnations_path(workspace, worker_id)
    # Same critical section as the appends: this rewrites the WHOLE list, so a
    # concurrent start_incarnation would otherwise be dropped by this write.
    with _appending(path):
        rows = incarnations(workspace, worker_id)
        for row in rows:
            if row["incarnation_id"] == incarnation_id:
                row["ended_at"], row["end_reason"] = _now(), end_reason
                _write(path, {"incarnations": rows})
                if current(workspace, worker_id).get("incarnation_id") == incarnation_id:
                    _write(current_path(workspace, worker_id),
                           {"session_id": None, "incarnation_id": None})
                return row
    raise IdentityError(f"no such incarnation: {incarnation_id!r}")


def tmux_session_name(worker_id: str) -> str:
    """Derived from the id, never the label — renaming a worker is a one-field
    edit, not a process migration. Look it up as `-t "=<name>"`: without the
    `=`, tmux prefix-matches and a short id attaches to the wrong worker."""
    return f"sutando-worker-{worker_id}"


def create_worker(workspace, *, runtime: str, host: str = "", cwd: str = "",
                  session_id=None, resume=False, fork_from=None,
                  transcript_path: str = "", tmux_socket: str = "") -> dict:
    """Mint a worker and open its first run.

    `resume` keeps the given session id and its lineage is the caller's to have
    recorded; `fork_from` records a new session descended from that one; neither
    means the new worker owns the earlier history — that stays with whoever ran it.
    """
    if resume and fork_from:
        raise IdentityError("resume and fork are different operations — pick one")
    if (resume or fork_from) and not session_id:
        raise IdentityError("resuming or forking needs the session id to act on")

    worker_id = new_worker_id()
    sid = session_id or uuid.uuid4().hex
    relation = RELATION_RESUMED if resume else (RELATION_FORKED if fork_from else RELATION_NEW)
    record_session(workspace, worker_id, sid, runtime=runtime, relation=relation,
                   parent_session_id=fork_from, host=host, cwd=cwd,
                   transcript_path=transcript_path)
    inc = start_incarnation(workspace, worker_id, sid, tmux_socket=tmux_socket,
                            tmux_session=tmux_session_name(worker_id))
    return {"worker_id": worker_id, "runtime_session_id": sid,
            "incarnation_id": inc["incarnation_id"], "relation": relation,
            "tmux_session": inc["tmux"]["session_name"]}
