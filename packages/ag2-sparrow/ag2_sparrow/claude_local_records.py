"""The claude-local session's two one-writer records and its per-root locks, shared by the adapter
and the bridge's card accepter (which reads the stall token), so it lives outside the adapter.

`state/claude-local-supervisor.json` is the launcher's derivation (generation tuple, consumer,
hitl pointer, stall token, deadline, incident, effect evidence), written under the supervisor
lock; `state/claude-local-plugin.json` is the Mod's progress record, written only by the packaged
`inbox progress` writer under `state/progress.lock`. Both writes are temp -> fsync -> replace ->
directory fsync, so a reader never sees a torn record and a failed fsync is reported, never hidden.
Stdlib only; imports no adapter.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

SUPERVISOR_NAME = "claude-local-supervisor.json"
PLUGIN_NAME = "claude-local-plugin.json"
PROGRESS_LOCK = "progress.lock"
CLAIMS_LOCK = "claims.lock"
SUPERVISOR_SCHEMA = 1
PLUGIN_SCHEMA = 1


class RecordError(Exception):
    """A record could not be written durably; the caller treats the write as not done."""


def state_dir(root) -> Path:
    return Path(root) / "state"


def supervisor_path(root) -> Path:
    return state_dir(root) / SUPERVISOR_NAME


def plugin_path(root) -> Path:
    return state_dir(root) / PLUGIN_NAME


def progress_lock_path(root) -> Path:
    return state_dir(root) / PROGRESS_LOCK


def claims_lock_path(root) -> Path:
    return state_dir(root) / CLAIMS_LOCK


def fsync_dir(path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_durable(path: Path, data: bytes) -> None:
    """temp -> file fsync -> os.replace -> directory fsync; any failure is RecordError with the
    temp removed and the previous record untouched (a failed directory fsync is reported too)."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".%s." % path.name, suffix=".tmp")
    except OSError as e:
        raise RecordError("%s: %s" % (path, e.strerror or e))
    try:
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            fsync_dir(path.parent)
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)
    except OSError as e:
        raise RecordError("%s: %s" % (path, e.strerror or e))


def read_json(path: Path) -> dict:
    """The record, {} when absent, RecordError when present but unreadable or not an object."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise RecordError("%s: %s" % (path, e.strerror or e))
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise RecordError("%s: not a JSON record (%s)" % (path, e))
    if not isinstance(rec, dict):
        raise RecordError("%s: not a JSON object" % path)
    return rec


@contextmanager
def flocked(path: Path, blocking: bool = True):
    """An exclusive flock on a stable file (never unlinked), created 0600 when absent."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── the supervisor record (the launcher's one derivation) ────────────────────

def empty_supervisor() -> dict:
    return {"schema": SUPERVISOR_SCHEMA, "generation": None, "consumer": None, "title_token": None,
            "launcher": None, "hitl": None, "stall": None, "deadline": None, "incident": None,
            "effect": None, "observed_session_id": None, "title_changed": None, "updated_at": 0.0}


def load_supervisor(root, path=None) -> dict:
    rec = read_json(supervisor_path(root) if path is None else path)
    if not rec:
        return empty_supervisor()
    out = empty_supervisor()
    out.update(rec)
    return out


def agent_problem(root, agent_id: str) -> str:
    """'' unless the root's record names another agent (or cannot be read): one root, one agent."""
    try:
        recorded = load_supervisor(root).get("agent_id")
    except RecordError as e:
        return "the work root's record is unreadable (%s)" % e
    if agent_id and recorded and recorded != agent_id:
        return ("the work root %s serves the agent %s, not %s; two agents never share a root, so give this one "
                "its own" % (root, recorded, agent_id))
    return ""


def save_supervisor(root, rec: dict) -> dict:
    rec = dict(rec, schema=SUPERVISOR_SCHEMA, updated_at=time.time())
    write_durable(supervisor_path(root), json.dumps(rec, sort_keys=True, indent=1).encode("utf-8"))
    return rec


def stall_token(stall) -> str:
    """The immutable token a card binds to: a hash of the stall evidence; '' when nothing is stalled."""
    if not stall:
        return ""
    evidence = {k: stall.get(k) for k in ("reason", "since", "frame_hash", "task", "checks")}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode("utf-8")).hexdigest()[:32]


# ── the plugin progress record (the Mod's, through the packaged writer only) ─

def empty_plugin() -> dict:
    return {"schema": PLUGIN_SCHEMA, "session_id": None, "nonce": None, "intake": "open", "reason": "",
            "inflight": {}, "generation": None, "updated_at": 0.0}


def load_plugin(root) -> dict:
    rec = read_json(plugin_path(root))
    if not rec:
        return empty_plugin()
    out = empty_plugin()
    out.update(rec)
    return out


def write_progress(root, mutate) -> dict:
    """The one progress writer: under state/progress.lock, load, `mutate(record)`, write durably.
    Every failure, the lock's own I/O included, is RecordError (the CLI turns it into a hard failure
    that closes the Mod's intake), so no caller sees a raw OSError."""
    try:
        with flocked(progress_lock_path(root)):
            rec = load_plugin(root)
            rec = mutate(rec) or rec
            rec = dict(rec, schema=PLUGIN_SCHEMA, updated_at=time.time())
            write_durable(plugin_path(root), json.dumps(rec, sort_keys=True).encode("utf-8"))
            return rec
    except OSError as e:
        raise RecordError("%s: %s" % (progress_lock_path(root), e.strerror or e))


def progress_gate_held(root) -> bool:
    """True while somebody holds state/progress.lock (a non-blocking probe, released at once)."""
    path = progress_lock_path(root)
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
