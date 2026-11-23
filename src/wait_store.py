"""Wait markers: one pending owner request that resumes once something outside the core finishes.

A wait is `<workspace>/state/<dirname>/<wait-id>.json`. Claiming it is a rename to
`<wait-id>.claimed`, so of a waiter, a deadline and a command exactly one wins and only the
winner acts; the claimed record keeps who took it and when. A waiter holds `<wait-id>.lock`
while it lives, so a dead one is seen and restarted. The winner writes one resume task, which
the core processes like any other task. `owner_task` is the one test of whose request may arm
a wait: a live owner-tier, non-collaborator task of the named source.

Skills supply what differs: the directory name, what a valid marker is and when it is ready.
Standard library only, plus local_task_protocol, task_body_guard and task_envelope.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import local_task_protocol as ltp
from task_body_guard import header_safe_value

WAIT_ID_RE = re.compile(r"^[0-9]{13}-[0-9a-f]{8}\Z")
MXID_RE = re.compile(r"^@[^\s:]+:[^\s]+\Z")
CLAIM_KEYS = ("claimed_by", "claimed_at")


class NotOwnerTask(ValueError):
    """The task may not arm a wait; str() is the reason, after the task's id."""


def now_iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_wait_id(t: float) -> str:
    return f"{int(t * 1000):013d}-{secrets.token_hex(4)}"


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def header_values(text: str, key: str) -> list[str]:
    """Every `key:` line anywhere in a task file, the body included."""
    prefix = f"{key}:"
    return [ln[len(prefix):].strip() for ln in text.splitlines() if ln.startswith(prefix)]


class WaitStore:
    """The wait files of one skill under one workspace. `valid(data, wait_id)` says whether a
    marker is one of this skill's; the path and JSON methods are the seams a caller may override."""

    def __init__(self, ws: Path, dirname: str, valid: Callable[[Any, str], bool]) -> None:
        self.ws, self.dirname, self.valid = Path(ws), dirname, valid

    def dir(self) -> Path:
        return self.ws / "state" / self.dirname

    def marker_path(self, wait_id: str) -> Path:
        return self.dir() / f"{wait_id}.json"

    def claimed_path(self, wait_id: str) -> Path:
        return self.dir() / f"{wait_id}.claimed"

    def lock_path(self, wait_id: str) -> Path:
        return self.dir() / f"{wait_id}.lock"

    def read_json(self, path: Path) -> Any:
        return read_json(path)

    def write_json(self, path: Path, data: dict) -> None:
        write_json(path, data)

    def read_marker(self, wait_id: str) -> dict | None:
        """The unclaimed marker, or None once it is claimed. Raises ValueError when unreadable."""
        try:
            data = json.loads(self.marker_path(wait_id).read_text())
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(str(exc)) from None
        if not self.valid(data, wait_id):
            raise ValueError(f"malformed wait marker {wait_id}")
        return data

    def list_markers(self) -> list[dict]:
        out: list[dict] = []
        d = self.dir()
        if not d.is_dir():
            return out
        for p in sorted(d.glob("*.json")):
            if not WAIT_ID_RE.match(p.stem):
                continue
            try:
                marker = self.read_marker(p.stem)
            except ValueError:
                continue
            if marker is not None:
                out.append(marker)
        return out

    def write_marker(self, marker: dict) -> Path:
        self.dir().mkdir(parents=True, exist_ok=True)
        final = self.marker_path(marker["wait_id"])
        self.write_json(final, marker)
        return final

    def claim(self, wait_id: str, by: str = "claim", at: float | None = None) -> dict | None:
        """Atomically take the wait. The claimed record, stamped with who took it and when,
        `{"wait_id", "invalid": True}` for a marker that is not valid, or None when someone else won."""
        try:
            os.rename(self.marker_path(wait_id), self.claimed_path(wait_id))
        except FileNotFoundError:
            return None
        data = self.read_json(self.claimed_path(wait_id))
        if not self.valid(data, wait_id):
            return {"wait_id": wait_id, "invalid": True}
        record = {**data, "claimed_by": by, "claimed_at": time.time() if at is None else at}
        try:
            self.write_json(self.claimed_path(wait_id), record)
        except OSError:
            pass  # the rename already decided the claim; only its report is lost
        return record

    def release(self, record: dict) -> bool:
        """Undo a claim whose resume task could not be written, so the next poll or rearm retries."""
        wait_id = record["wait_id"]
        try:
            self.write_json(self.claimed_path(wait_id), {k: v for k, v in record.items() if k not in CLAIM_KEYS})
        except OSError:
            pass
        try:
            os.rename(self.claimed_path(wait_id), self.marker_path(wait_id))
        except OSError:
            return False
        return True

    def acquire_lock(self, wait_id: str):
        """An exclusive, non-blocking hold on the wait's lock file; None when a live waiter has it."""
        self.dir().mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path(wait_id), "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        return fh

    def waiter_alive(self, wait_id: str) -> bool:
        fh = self.acquire_lock(wait_id)
        if fh is None:
            return True
        fh.close()
        return False

    def prune(self, now: float, older_than_s: float) -> int:
        """Drop claimed records, and locks whose marker is gone, last touched `older_than_s` ago."""
        removed = 0
        d = self.dir()
        if not d.is_dir():
            return removed
        for p in d.iterdir():
            if p.suffix not in (".claimed", ".lock") or (p.suffix == ".lock" and self.marker_path(p.stem).exists()):
                continue
            try:
                if now - p.stat().st_mtime >= older_than_s:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


def hold_lock(store: WaitStore, wait_id: str):
    """The waiter's lock with this process's pid written in it, or None when another waiter holds it."""
    lock = store.acquire_lock(wait_id)
    if lock is None:
        return None
    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    return lock


def spawn_detached(script: Path, ws: Path, wait_id: str, log_name: str) -> int:
    """Start `script --workspace WS waiter WAIT_ID` in its own session, so it outlives the core."""
    logs = Path(ws) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / log_name, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, str(script), "--workspace", str(ws), "waiter", wait_id],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(ws),
            start_new_session=True,
            close_fds=True,
        )
    return proc.pid


def _stamp(text: str, ws: Path) -> str:
    try:
        import task_envelope  # noqa: PLC0415

        return task_envelope.stamp_text(text, ws)
    except Exception:  # noqa: BLE001 — a stamping error costs the stamp, never the task
        return text


def write_resume_task(ws: Path, task_id: str, *, source: str, room: str, owner: str, body: str,
                      now: float) -> Path:
    """One owner-tier task for the core, addressed to the wait's room. `body` is already confined."""
    headers = [
        ("id", task_id),
        ("timestamp", now_iso(now)),
        ("source", source),
        ("interaction_type", "message"),
        ("channel_id", header_safe_value(room)),
        ("user_id", header_safe_value(owner)),
        ("access_tier", "owner"),
        ("priority", "normal"),
    ]
    ltp.set_task_stamper(lambda text: _stamp(text, ws))
    try:
        return ltp.write_task_file(Path(ws) / "tasks", task_id, headers, body)
    finally:
        ltp.set_task_stamper(None)


def owner_task(ws: Path, task: str, source: str, source_name: str = "") -> tuple[str, str]:
    """(owner mxid, task text) of `task` when it is a live owner-tier, non-collaborator task from
    `source` (`source_name` in the refusal). Anything else raises NotOwnerTask: only the owner's own request may arm an owner resume."""
    try:
        text = (Path(ws) / "tasks" / f"{task}.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise NotOwnerTask("is not a live task in this workspace") from None
    above_body = ltp.parse_task_headers(text).headers
    if above_body.get("id") != task:
        raise NotOwnerTask("does not carry its own id")
    # The gateway writes source above the body and one-lines every value; any other
    # header line anywhere in the file counts against the task, never for it.
    if above_body.get("source") != source or set(header_values(text, "source")) != {source}:
        raise NotOwnerTask(f"is not from {source_name or source}")
    if header_values(text, "collaborator"):
        raise NotOwnerTask("is a collaborator task")
    tiers = {ltp.canonical_access_tier(v) for v in header_values(text, "access_tier")}
    if tiers != {"owner"}:
        raise NotOwnerTask("is not owner tier")
    users = set(header_values(text, "user_id"))
    if len(users) != 1 or not MXID_RE.match(next(iter(users))):
        raise NotOwnerTask("has no single user_id")
    try:
        import task_envelope  # noqa: PLC0415

        verdict = task_envelope.verify_text(text, ws).get("verdict")
    except Exception:  # noqa: BLE001 — an unverifiable stamp is soak-mode telemetry, not a refusal
        verdict = None
    if verdict == "invalid":
        raise NotOwnerTask("has an envelope stamp that does not verify")
    return next(iter(users)), text
