#!/usr/bin/env python3
"""Which conversation this host's core is having, and which it had before.

`state/cores/<host>.alive` answers liveness only — its `session` field is the
tmux session NAME, not the runtime conversation id — so a rebooted host could
bring its WORKERS back on their own history (worker_identity records it) and
could not do the same for the core. This is the core's half of that record,
deliberately the same shape as the worker's: sessions (which conversations),
runs (which incarnations), current (which one is live).

Per host, beside the heartbeat's own file, because two cores on two machines
share a workspace and must not overwrite each other's lineage.

An unknown session is recorded as nothing, never as a placeholder: the writer
is a detached heartbeat that may not inherit CLAUDE_CODE_SESSION_ID, and a
fabricated id would later resume the wrong conversation.
"""
from __future__ import annotations

import fcntl
import contextlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

__all__ = ["lineage_dir", "sessions", "runs", "current", "record_run",
           "transcript_path_for"]


def lineage_dir(workspace, host: str) -> Path:
    return Path(workspace) / "state" / "cores" / f"{host}.lineage"


def _path(workspace, host: str, name: str) -> Path:
    return lineage_dir(workspace, host) / name


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, payload) -> None:
    """Temp-file + replace: a reader polling mid-write must never see a
    half-written file, which a truncating open would show as empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        os.replace(tmp, path)
    except BaseException:
        # Suppressed: a failing unlink would replace the write error that
        # actually explains the failure.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _appending(path: Path):
    """Serialise read-modify-write; `_write` alone protects only the reader."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    with open(lock, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def sessions(workspace, host: str) -> list:
    return _read(_path(workspace, host, "sessions.json"), {}).get("sessions", [])


def runs(workspace, host: str) -> list:
    return _read(_path(workspace, host, "runs.json"), {}).get("runs", [])


def current(workspace, host: str) -> dict:
    return _read(_path(workspace, host, "current.json"),
                 {"session_id": None, "run_id": None})


def transcript_path_for(workspace, cwd: str, session_id: str) -> str:
    """Where the runtime keeps this session's transcript, or "" if not there.

    Empty means "no file", never "unknown": a path to a file nobody wrote turns
    a not-yet-started session into a lost one.
    """
    if not (cwd and session_id):
        return ""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    f = Path(workspace) / ".claude-sutando" / "projects" / slug / f"{session_id}.jsonl"
    return str(f) if f.is_file() else ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record_run(workspace, host: str, session_id: str, *, runtime: str = "",
               cwd: str = "", tmux_socket: str = "", tmux_session: str = "") -> dict:
    """Note that this host's core is running `session_id`, idempotently.

    Called on every heartbeat, so a beat on the session already current is a
    no-op — otherwise the record would log beats rather than restarts.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return current(workspace, host)
    if current(workspace, host).get("session_id") == session_id:
        return current(workspace, host)

    spath = _path(workspace, host, "sessions.json")
    with _appending(spath):
        rows = sessions(workspace, host)
        if not any(r.get("session_id") == session_id for r in rows):
            rows.append({"session_id": session_id, "runtime": runtime,
                         "first_seen": _now(),
                         "transcript": {"host": host, "cwd": cwd,
                                        "path": transcript_path_for(workspace, cwd,
                                                                    session_id)}})
            _write(spath, {"sessions": rows})

    run = {"run_id": uuid.uuid4().hex, "session_id": session_id,
           "started_at": _now(),
           "tmux": {"socket": tmux_socket, "session_name": tmux_session}}
    rpath = _path(workspace, host, "runs.json")
    with _appending(rpath):
        rows = runs(workspace, host)
        # The early return above is a check-then-act OUTSIDE this lock, so two
        # callers can reach here for one session; the newest row settles it.
        if not rows or rows[-1].get("session_id") != session_id:
            rows.append(run)
            _write(rpath, {"runs": rows})
        else:
            run = rows[-1]

    _write(_path(workspace, host, "current.json"),
           {"session_id": session_id, "run_id": run["run_id"]})
    return current(workspace, host)
