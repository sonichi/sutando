"""The AG2 Space agent a bridge serves, and the one path segment a per-agent work root is named by.

`resolve` is the bridge's identity chain, in its order: the process env (`AGENT_MXID`, `AGENT_ID`),
then the channel .env files (`AGENT_MXID`, `AGENT_ID`, `AG2SPACE_USER_ID`), then the durable binding
`<state>/auth/ag2space.json`'s `agent_id`, which a seat (strict) never reads. The gateway bridge and the installers read it here, so an
install derives its root from exactly the identity the bridge will report. A launcher that has
decided the identity sets `PINNED_KEY` (see `launch_environ`): the chain is then `AGENT_MXID` alone,
so the bridge runs as the launcher's saved identity or as none, never one a file or binding supplies.
A .env file's lines are parsed by the credential owner, gateway_credential_sources. Stdlib only.

`publish_lane` is the one writer of `<state>/auth/lane-identity[.<instance>].json`: the id the live
drain of that gateway instance judges rooms by, with its instance, pid and a timestamp, never a
credential. `lane_state` reads it back as fresh, stale, absent, invalid or unreadable, and
`published_lane_record` is its fresh record; `retract_lane` invalidates it. All three hold the
instance's `lane-identity[.<instance>].lock` beside it, and a record that cannot be removed is
tombstoned or emptied, so a failed invalidation reads as invalid, never as a live record.

`root_segment` maps an id to one path segment, lowercase `[a-z0-9._-]`, no leading dot, at most
`SEGMENT_MAX` bytes. A plain MXID `@local:server` whose parts are lowercase `[a-z0-9.-]` (first
character alphanumeric) becomes `local_server`: exactly one `_`, so the id reads back from it. Any
other id becomes a readable prefix plus `__` and 12 hex of its sha256, so it never equals a plain
one (which has no `__`) and two such ids differ by their digest.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path

from . import bounded_file_read
from . import gateway_credential_sources as _sources
from .outbox import valid_pid
from .persisted_number import bounded_number
from .gateway_instance import suffix as _instance_suffix
from .task_io import start_time

ENV_KEYS = ("AGENT_MXID", "AGENT_ID")
FILE_KEYS = ("AGENT_MXID", "AGENT_ID", "AG2SPACE_USER_ID")
PINNED_KEY = "AG2_IDENTITY_PINNED"
SEGMENT_MAX = 64
DIGEST_HEX = 12
_PLAIN = re.compile(r"@([a-z0-9][a-z0-9.-]*):([a-z0-9][a-z0-9.-]*)")
_UNSAFE = re.compile(r"[^a-z0-9._-]")


# Parsing has one owner, so the identity tiers cannot disagree with the credential tiers.
unquote_env = _sources.unquote
file_value = _sources.config_from_files


def env_files(paths) -> list:
    """Readable .env files in the given order, as [(path, {key: value})]; unreadable ones are skipped."""
    out = []
    for path in paths:
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        out.append((path, _sources.parse_env_text(text)))
    return out


def enrolled(state_dir) -> str:
    """The binding's `agent_id`, '' when absent, unreadable, malformed or not a string."""
    if state_dir is None:
        return ""
    try:
        rec = json.loads((Path(state_dir) / "auth" / "ag2space.json").read_text())
        v = rec.get("agent_id")
        return v.strip() if isinstance(v, str) else ""
    except Exception:
        return ""


def pinned_environ(mxid: str) -> dict:
    """The variables that pin a bridge's identity to `mxid`, or to none when `mxid` is ''."""
    return {PINNED_KEY: "1", **({"AGENT_MXID": mxid} if mxid else {})}


def launch_environ(base, mxid: str) -> dict:
    """`base` with its identity variables replaced by `pinned_environ(mxid)`, for a launched bridge."""
    env = {k: v for k, v in base.items() if k not in ENV_KEYS and k != PINNED_KEY}
    env.update(pinned_environ(mxid))
    return env


try:
    import fcntl
except ImportError:  # Windows: no lane record; Hatch, the only reader, is POSIX-only
    fcntl = None
LANE_SUPPORTED = fcntl is not None
_LANE_MAX_BYTES = 4096
# A record is a heartbeat: the live drain rewrites it at least every LANE_REFRESH_S, and a reader
# treats one older than LANE_FRESH_S (or dated past LANE_SKEW_S ahead) as missing.
LANE_REFRESH_S = 10.0
LANE_FRESH_S = 30.0
LANE_SKEW_S = 5.0
# A ts beyond this is not a time any writer produced; it reads as missing, never raises.
_LANE_TS_MAX = 2**53


def _lane_name(instance: str) -> str:
    return f"lane-identity{_instance_suffix(instance)}.json"


def _auth_dir(state_dir, create: bool) -> "int | None":
    """An fd on `<state>/auth`, opened without following a symlink. Absent: None, or with `create`
    it is made again a few times and then OSError, so no caller ever gets None to mean the CWD."""
    if not LANE_SUPPORTED:
        raise OSError("no lane record on this platform")
    path = Path(state_dir) / "auth"
    for _ in range(3 if create else 1):
        if create:
            path.mkdir(parents=True, exist_ok=True)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue  # removed after the mkdir
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(f"{path} is a symlink or not a directory")
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if os.fstat(fd).st_ino != st.st_ino:
            os.close(fd)
            raise OSError(f"{path} changed while it was opened")
        return fd
    if create:
        raise OSError(f"{path} kept disappearing while it was created")
    return None


def _close(fd) -> None:
    if isinstance(fd, int) and not isinstance(fd, bool):
        os.close(fd)


@contextlib.contextmanager
def _lane_locked(dfd: int, instance: str):
    """Exclusive lock on the instance's own `.lock` beside its record: every publish, retraction and
    read of that record is serialized, so a check and the write or unlink after it are one step."""
    name = _lane_name(instance)[:-len(".json")] + ".lock"
    for attempt in range(5):
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            break
        except FileNotFoundError:  # APFS: racing O_CREATs of one name can see ENOENT
            if attempt == 4:
                raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


LANE_FRESH, LANE_STALE = "fresh", "stale"
LANE_ABSENT, LANE_INVALID, LANE_UNREADABLE = "absent", "invalid", "unreadable"


def _read_at(dfd: int, name: str) -> "tuple[dict | None, str]":
    """(record, "") or (None, LANE_ABSENT / LANE_INVALID / LANE_UNREADABLE). It never blocks: a
    FIFO, a symlink or any other non-regular file is refused as invalid before any read."""
    text, why = bounded_file_read.read_regular(name, _LANE_MAX_BYTES, dir_fd=dfd, follow=False)
    if text is None:
        if not why:
            return None, LANE_ABSENT
        return None, LANE_UNREADABLE if bounded_file_read.unreadable(why) else LANE_INVALID
    try:
        rec = json.loads(text)
    except ValueError:
        return None, LANE_INVALID
    return (rec, "") if isinstance(rec, dict) else (None, LANE_INVALID)


def _write_at(dfd: int, name: str, record: dict) -> None:
    """Write `record` to a fresh temp, fsync it, then replace `name`; OSError on failure."""
    tmp = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
    try:
        try:
            view = memoryview(json.dumps(record).encode("utf-8"))
            while view:  # a short write is legal: the record is whole or not published
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dfd)


def _invalidate_at(dfd: int, name: str, instance: str) -> None:
    """Make the record read as missing: unlink it, else replace it with a tombstone, else empty it
    in place (which needs no free space); OSError only when all three fail."""
    try:
        os.unlink(name, dir_fd=dfd)
        return
    except FileNotFoundError:
        return
    except OSError:
        pass
    try:
        _write_at(dfd, name, {"instance": instance or "", "retracted": True})
        return
    except OSError:
        pass
    fd = os.open(name, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=dfd)
    os.close(fd)


def _poison_at(dfd: int, name: str) -> None:
    """Leave the record empty in place, or create it empty: it reads invalid, never absent, so a
    failed publication is not taken for a drain that never publishes. Truncation needs no space."""
    os.close(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=dfd))


_OWN_START: dict = {}


def own_start() -> "str | None":
    """This process's start time, which binds what it writes to it rather than to its pid."""
    pid = os.getpid()
    if pid not in _OWN_START:
        _OWN_START.clear()
        _OWN_START[pid] = start_time(pid)
    return _OWN_START[pid]


def _well_formed(rec: dict, instance: str) -> "tuple[dict, float] | None":
    if rec.get("instance") != (instance or "") or rec.get("retracted"):
        return None
    pid, agent = rec.get("pid"), rec.get("agent_id")
    ts = bounded_number(rec.get("ts"), -_LANE_TS_MAX, _LANE_TS_MAX)
    if not isinstance(agent, str) or valid_pid(pid) is None or ts is None:
        return None
    return {**rec, "agent_id": agent.strip()}, ts


def publish_lane(state_dir, instance: str, agent_id: str, send_lock: "int | None" = None) -> dict:
    """Record, under the lane lock, the identity the live drain of `instance` resolved, so a reader
    that must not open the credential env sees that drain's lane; returns the record written.
    It holds the id, instance, pid, the process's start time (which binds it to that process, not
    to a reused pid), a timestamp and, when given, the proactive `send_lock` protocol the drain
    honours. On a failed write the record is emptied in the same locked step, so it reads invalid,
    and OSError is raised either way."""
    if valid_pid(os.getpid()) is None:
        raise OSError(f"pid {os.getpid()} is outside the pid domain readers accept")
    record = {"agent_id": (agent_id or "").strip(), "instance": instance or "",
              "pid": os.getpid(), "start": own_start(), "ts": time.time(),
              **({"send_lock": send_lock} if send_lock is not None else {})}
    dfd = _auth_dir(state_dir, create=True)
    try:
        with _lane_locked(dfd, instance):
            try:
                _write_at(dfd, _lane_name(instance), record)
            except OSError:
                with contextlib.suppress(OSError):
                    _poison_at(dfd, _lane_name(instance))
                raise
    finally:
        _close(dfd)
    return record


def retract_lane(state_dir, instance: str, pid: "int | None" = None, ts: "float | None" = None) -> None:
    """Invalidate `instance`'s record; with `pid` (and `ts`), only the record that publisher wrote,
    checked and removed under the lane lock. Never raises."""
    with contextlib.suppress(OSError):
        dfd = _auth_dir(state_dir, create=False)
        if dfd is None:
            return
        try:
            with _lane_locked(dfd, instance):
                if pid is not None:
                    rec = _read_at(dfd, _lane_name(instance))[0] or {}
                    if rec.get("instance") != (instance or "") or rec.get("pid") != pid \
                            or (ts is not None and rec.get("ts") != ts):
                        return
                _invalidate_at(dfd, _lane_name(instance), instance)
        finally:
            _close(dfd)


def owned_by(rec: "dict | None", pid: int, acquired_at=None) -> bool:
    """`rec` (a lane record or a lock's declarations) was written by the live process `pid`
    itself, not by an earlier one that had its pid: its start time is that process's, or, with no
    start time to compare, its `ts` postdates the lock taken at `acquired_at`."""
    if not isinstance(rec, dict) or rec.get("pid") != pid:
        return False
    live, start = start_time(pid), rec.get("start")
    if live is not None and isinstance(start, str):
        return start == live
    ts, acquired = rec.get("ts"), acquired_at
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (ts, acquired)) \
        and ts >= acquired - 1


def lane_state(state_dir, instance: str, now: "float | None" = None) -> "tuple[str, dict | None]":
    """(state, record) of `instance`'s lane record, read under the lane lock. LANE_FRESH or
    LANE_STALE (ts outside LANE_FRESH_S of `now`, or over LANE_SKEW_S ahead) carry the well-formed
    record: agent_id a str, its instance, a valid_pid, a finite ts, not retracted. Otherwise None
    with LANE_ABSENT (no file), LANE_INVALID (a file that is no such record: corrupt, a tombstone,
    a symlink, not regular) or LANE_UNREADABLE (it could not be read, which proves nothing)."""
    if state_dir is None:
        return LANE_ABSENT, None
    try:
        dfd = _auth_dir(state_dir, create=False)
        if dfd is None:
            return LANE_ABSENT, None
        try:
            with _lane_locked(dfd, instance):
                rec, why = _read_at(dfd, _lane_name(instance))
        finally:
            _close(dfd)
    except OSError:
        return LANE_UNREADABLE, None
    if why:
        return why, None
    formed = _well_formed(rec, instance)
    if formed is None:
        return LANE_INVALID, None
    rec, ts = formed
    now = time.time() if now is None else now
    # A drain that can no longer write cannot refresh it.
    return (LANE_FRESH if now - LANE_FRESH_S <= ts <= now + LANE_SKEW_S else LANE_STALE), rec


def published_lane_record(state_dir, instance: str, now: "float | None" = None) -> "dict | None":
    """`instance`'s lane record when lane_state reads it LANE_FRESH, else None. Whether the pid is
    the live drain is the reader's check."""
    state, rec = lane_state(state_dir, instance, now)
    return rec if state == LANE_FRESH else None


def resolve(state_dir, channel_value, environ=None, strict=None) -> str:
    """The agent id by the bridge's chain; `channel_value(key)` reads the channel .env tier. '' = unknown.
    A pinned environment answers from its own `AGENT_MXID` alone. `strict` (default: the environment is a
    seat's) stops at the channel tier: the host's binding is the host's agent, never a seat's."""
    env = os.environ if environ is None else environ
    if strict is None:
        strict = _sources.is_seat(env)
    if env.get(PINNED_KEY):
        return (env.get("AGENT_MXID") or "").strip()
    for key in ENV_KEYS:
        v = (env.get(key) or "").strip()
        if v:
            return v
    for key in FILE_KEYS:
        v = (channel_value(key) or "").strip()
        if v:
            return v
    return "" if strict else enrolled(state_dir)


def root_segment(agent_id: str) -> str:
    """The one path segment for `agent_id`; ValueError on an empty id."""
    raw = (agent_id or "").strip()
    if not raw:
        raise ValueError("the agent id is empty")
    m = _PLAIN.fullmatch(raw)
    if m:
        plain = f"{m.group(1)}_{m.group(2)}"
        if len(plain) <= SEGMENT_MAX:
            return plain
    readable = _UNSAFE.sub("-", raw.lower().lstrip("@").replace(":", "_")).lstrip("._-")
    readable = readable[:SEGMENT_MAX - DIGEST_HEX - 2].rstrip("._-") or "agent"
    return f"{readable}__{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:DIGEST_HEX]}"
