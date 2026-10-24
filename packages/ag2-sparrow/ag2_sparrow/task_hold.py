"""A deliberate hold on an open task: a machine-readable marker naming why it waits.

An adapter places it when its worker parks a task on purpose; health readers read it
so a held task is reported as held, never as one nobody picked up. Every operation is
confined to the literal RESULT_DIR/.task-holds directory and never follows a symlink;
only a whole, well-formed marker counts, anything else is not a hold."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path

from . import gateway_instance, local_task_protocol
from .persisted_number import bounded_number

HOLD_DIR = ".task-holds"
LOCK_NAME = ".lock"
OWNER_GO = "owner-go"
_REASON_RE = re.compile(r"[a-z0-9-]{1,40}")
_MAX_BYTES = 4096
# Epoch seconds before year 5138: bounds `since` to a sane, finite range.
_MAX_EPOCH = 1e11
SINCE_SKEW_S = 5.0


def valid_tid(tid: str) -> bool:
    """A live task id, optionally under a named gateway instance (`task-<inst>~<id>`)."""
    if not isinstance(tid, str):
        return False
    if local_task_protocol.valid_task_id(tid):
        return True
    inst, sep, broker = tid[len("task-"):].partition("~")
    return (tid.startswith("task-") and bool(sep)
            and gateway_instance.INSTANCE_RE.fullmatch(inst) is not None
            and local_task_protocol.valid_task_id(broker))


def _name(tid: str) -> str:
    if not valid_tid(tid):
        raise ValueError(f"not a task id: {tid!r}")
    return f"{tid}.json"


def _valid_since(value) -> bool:
    """A hold time a writer produced: a bounded number, not ahead of now by more than SINCE_SKEW_S
    (a far-future time would never age, so it is no hold)."""
    since = bounded_number(value, 0, _MAX_EPOCH)
    return since is not None and since <= time.time() + SINCE_SKEW_S


def _open_dir(result_dir: Path, create: bool) -> "int | None":
    """An fd on the literal hold dir; None when it does not exist. A symlink or a
    non-directory there raises ValueError: nothing is ever read or written through it."""
    path = Path(result_dir) / HOLD_DIR
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"{path} is a symlink or not a directory")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if os.fstat(fd).st_ino != st.st_ino:
        os.close(fd)
        raise ValueError(f"{path} changed while it was opened")
    return fd


def _read_at(dfd: int, name: str) -> "dict | None":
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, _MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        record = json.loads(raw.decode("utf-8"), parse_constant=lambda c: None)
    except (UnicodeDecodeError, ValueError):
        return None
    if not (isinstance(record, dict) and isinstance(record.get("reason"), str)
            and _REASON_RE.fullmatch(record["reason"]) and _valid_since(record.get("since"))
            and isinstance(record.get("token"), str) and record["token"]):
        return None
    return record


def place(result_dir: Path, tid: str, reason: str = OWNER_GO,
          now: "float | None" = None) -> "str | None":
    """Write the marker whole and exclusively in one step; its token, or None when one exists."""
    name = _name(tid)
    since = time.time() if now is None else now
    if not _REASON_RE.fullmatch(reason):
        raise ValueError(f"hold reason must be a [a-z0-9-]{{1,40}} slug: {reason!r}")
    if not _valid_since(since):
        raise ValueError(f"hold since must be a finite epoch: {since!r}")
    token = secrets.token_hex(16)
    body = json.dumps({"reason": reason, "since": since, "token": token}).encode("utf-8")
    return token if _create_at(result_dir, name, body) else None


def _held_dir(result_dir: Path, create: bool) -> int:
    """`_open_dir`, but a dir that is absent (or vanished mid-open) raises: a relative
    operation never falls back to the process CWD."""
    dfd = _open_dir(result_dir, create)
    if dfd is None:
        raise FileNotFoundError(f"{Path(result_dir) / HOLD_DIR} is absent")
    return dfd


def _create_at(result_dir: Path, name: str, body: bytes) -> bool:
    """Write `name` whole and exclusively under the held dir fd; False when it exists."""
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    dfd = _held_dir(result_dir, create=True)
    published = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=dfd)
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            with _locked(dfd):
                os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
                published = True
        except FileExistsError:
            return False
        except OSError:
            if not published:
                raise  # once linked, the whole record stands whatever cleanup does
        _quietly(os.fsync, dfd)
        return True
    finally:
        _quietly(os.unlink, tmp, dir_fd=dfd)  # a leftover dot-temp never reads as a record
        _quietly(os.close, dfd)


def _quietly(op, *args, **kwargs) -> None:
    try:
        op(*args, **kwargs)
    except OSError:
        pass


def read(result_dir: Path, tid: str) -> "dict | None":
    """{"reason", "since", "token"} for a whole, well-formed marker; None for anything else."""
    try:
        name = _name(tid)
        dfd = _open_dir(result_dir, create=False)
    except (ValueError, OSError):
        return None
    if dfd is None:
        return None
    try:
        return _read_at(dfd, name)
    finally:
        os.close(dfd)


@contextlib.contextmanager
def _locked(dfd: int):
    """Exclusive lock on the hold dir's own `.lock` (opened through `dfd`, never a symlink):
    every create and owned unlink in it is serialized against each other."""
    fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _unlink_owned(dfd: int, name: str, token: "str | None") -> bool:
    """Unlink `name` when `token` is None or is the token the file records; under the
    dir lock, so the file read is the file removed."""
    with _locked(dfd):
        if token is not None:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
            except OSError:
                return False
            try:
                raw = os.read(fd, _MAX_BYTES + 1)
            except OSError:
                return False
            finally:
                os.close(fd)
            try:
                record = json.loads(raw.decode("utf-8"), parse_constant=lambda c: None)
            except (UnicodeDecodeError, ValueError):
                return False
            if not (isinstance(record, dict) and record.get("token") == token):
                return False
        try:
            os.unlink(name, dir_fd=dfd)
        except FileNotFoundError:
            return False
        return True


def release(result_dir: Path, tid: str, token: "str | None" = None) -> bool:
    """Remove the marker; with `token`, only the hold that token placed. True when removed."""
    name = _name(tid)
    dfd = _open_dir(result_dir, create=False)
    if dfd is None:
        return False
    try:
        return _unlink_owned(dfd, name, token)
    finally:
        os.close(dfd)


def _notice_name(tid: str) -> str:
    return f"{_name(tid)[:-len('.json')]}.notice"


def mark_notice(result_dir: Path, tid: str) -> "str | None":
    """Record, durably and once, that this caller queues the task's one notice: its token,
    or None when another caller already did."""
    token = secrets.token_hex(16)
    body = json.dumps({"token": token}).encode("utf-8")
    return token if _create_at(result_dir, _notice_name(tid), body) else None


def clear_notice(result_dir: Path, tid: str, token: str) -> None:
    """Forget this caller's notice record, whose notice was never queued."""
    dfd = _held_dir(result_dir, create=False)
    try:
        _unlink_owned(dfd, _notice_name(tid), token)
    finally:
        os.close(dfd)


def notice_marked(result_dir: Path, tid: str) -> bool:
    """Whether a notice was ever queued for this task; an unreadable dir counts as queued."""
    name = _notice_name(tid)
    try:
        dfd = _open_dir(result_dir, create=False)
    except (OSError, ValueError):
        return True
    if dfd is None:
        return False
    try:
        os.stat(name, dir_fd=dfd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True
    finally:
        os.close(dfd)


def prune(result_dir: Path, task_dir: Path) -> None:
    """Drop the holds of tasks no longer in `task_dir` (answered and archived)."""
    dfd = _open_dir(result_dir, create=False)
    if dfd is None:
        return
    try:
        for name in os.listdir(dfd):
            tid = next((name[:-len(x)] for x in (".json", ".notice") if name.endswith(x)), "")
            if valid_tid(tid) and not (Path(task_dir) / f"{tid}.txt").exists():
                _unlink_owned(dfd, name, None)
    finally:
        os.close(dfd)
