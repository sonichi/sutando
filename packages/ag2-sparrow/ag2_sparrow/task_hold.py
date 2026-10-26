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

from .delivery_core.contract import Defer

from . import bounded_file_read, gateway_instance, local_task_protocol
from .persisted_number import bounded_number

HOLD_DIR = ".task-holds"
LOCK_NAME = ".lock"
OWNER_GO = "owner-go"
_REASON_RE = re.compile(r"[a-z0-9-]{1,40}")
_MAX_BYTES = 4096
# A hold line about a task is named with its notice generation, far below any message timestamp.
NOTICE_GEN_LIMIT = 10**6
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


def _json_at(dfd: int, name: str) -> "tuple[dict | None, str]":
    """(object, "") or (None, why): "" absent, else read_regular's refusal or "not a record"."""
    text, why = bounded_file_read.read_regular(name, _MAX_BYTES, dir_fd=dfd, follow=False)
    if text is None:
        return None, why
    try:
        record = json.loads(text, parse_constant=lambda c: None)
    except ValueError:
        return None, "not a record"
    return (record, "") if isinstance(record, dict) else (None, "not a record")


def _read_at(dfd: int, name: str) -> "dict | None":
    return _marker(_json_at(dfd, name)[0])


def _marker(record) -> "dict | None":
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


def read_checked(result_dir: Path, tid: str) -> "dict | None":
    """`read`, but an unreadable hold dir or marker raises OSError instead of reading as None:
    one that cannot be read now is not one that is absent."""
    name = _name(tid)
    dfd = _open_dir(result_dir, create=False)
    if dfd is None:
        return None
    try:
        record, why = _json_at(dfd, name)
        if record is None and bounded_file_read.unreadable(why):
            raise OSError(f"the hold marker of {tid} is {why}")
        return _marker(record)
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
        if token is not None and (_json_at(dfd, name)[0] or {}).get("token") != token:
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
    """Record, durably and once, that this caller queues the task's first notice, generation 0,
    not yet armed: its token, or None when another caller already did."""
    token = secrets.token_hex(16)
    body = json.dumps({"token": token, "gen": 0, "armed": False}).encode("utf-8")
    return token if _create_at(result_dir, _notice_name(tid), body) else None


def _notice_fields(record: dict) -> dict:
    gen = record.get("gen")
    if not (isinstance(record.get("token"), str) and isinstance(record.get("armed", False), bool)
            and (gen is None or (type(gen) is int and 0 <= gen < NOTICE_GEN_LIMIT))):
        raise ValueError("the notice record is not one a hold wrote")
    return {"token": record["token"], "gen": gen, "armed": record.get("armed", False)}


def notice_record(result_dir: Path, tid: str) -> "dict | None":
    """{"token", "gen", "armed"} of the task's notice record, None when it has none. "gen" is
    None for a record from before generations. OSError or ValueError when it cannot be read."""
    name = _notice_name(tid)
    dfd = _open_dir(result_dir, create=False)
    if dfd is None:
        return None
    try:
        record, why = _json_at(dfd, name)
    finally:
        os.close(dfd)
    if record is None:
        if not why:
            return None
        raise OSError(f"the notice record of {tid} is unusable: {why}")
    return _notice_fields(record)


def _rewrite_notice(result_dir: Path, tid: str, token: str, change) -> dict:
    """Replace the notice record `token` names with `change(fields)`, in one locked step."""
    name = _notice_name(tid)
    dfd = _held_dir(result_dir, create=False)
    try:
        with _locked(dfd):
            record, why = _json_at(dfd, name)
            fields = _notice_fields(record) if record is not None else None
            if fields is None or fields["token"] != token or fields["gen"] is None:
                raise ValueError(f"the notice record of {tid} changed ({why or 'another token'})")
            new = change(fields)
            tmp = f".{name}.{secrets.token_hex(8)}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            try:
                try:
                    view = memoryview(json.dumps(new).encode("utf-8"))
                    while view:  # a short write is legal: the record is whole or not replaced
                        view = view[os.write(fd, view):]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            finally:
                _quietly(os.unlink, tmp, dir_fd=dfd)
            return new
    finally:
        os.close(dfd)


def advance_notice(result_dir: Path, tid: str, token: str) -> "tuple[str, int]":
    """Retire the record's unarmed generation for the next one: (new token, new gen)."""
    new_token = secrets.token_hex(16)

    def nxt(fields):
        if fields["armed"] or fields["gen"] + 1 >= NOTICE_GEN_LIMIT:
            raise ValueError(f"the notice of {tid} is armed or out of generations")
        return {"token": new_token, "gen": fields["gen"] + 1, "armed": False}
    return new_token, _rewrite_notice(result_dir, tid, token, nxt)["gen"]


def arm_notice(result_dir: Path, tid: str, token: str) -> None:
    """Mark the record's generation as the one line a hold stands on, which a drain may send."""
    _rewrite_notice(result_dir, tid, token, lambda f: {**f, "armed": True})


def disarm_notice(result_dir: Path, tid: str, token: str) -> None:
    """Take back the arming of the record's generation, whose hold was never placed."""
    _rewrite_notice(result_dir, tid, token, lambda f: {**f, "armed": False})


def line_veto(result_dir: Path, tid: str, ts: "int | None") -> "str | None":
    """Asked at a drain's send of a message about `tid` named with `ts`: why a hold line (ts below
    NOTICE_GEN_LIMIT) must not be sent, or None. Only the record's own generation, once armed,
    with its hold standing, is sent; a record from before generations needs its hold standing."""
    if ts is None or not 0 <= ts < NOTICE_GEN_LIMIT:
        return None
    try:
        fields = notice_record(result_dir, tid)
        held = read_checked(result_dir, tid)
    except (OSError, ValueError) as e:
        # Unreadable is not absent: the drain keeps the line for a later pass.
        return Defer(f"its hold could not be checked ({e})")
    if fields is not None and fields["gen"] is None:
        return None if held is not None else "no hold stands on it"
    if fields is None or fields["gen"] != ts:
        return "a later hold line replaced it, or no hold queued it"
    if not fields["armed"] or held is None:
        return "no hold stands on it"
    return None


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
