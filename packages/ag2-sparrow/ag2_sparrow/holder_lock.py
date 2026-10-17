"""One-process exclusion per work root: a flock held for the owner's life, and a record of who holds it.

The contract is docs/design-holder-lock.md. The flock alone decides who may acquire; the record only
explains who holds and lets a successor see a dead or live predecessor. The lock directory lives
outside the work root, under the effective uid's home from pwd (never the environment), so every
launch context of one user resolves one lock for one real work root. POSIX only. Stdlib only;
vendored byte-identical into the ag2-sparrow package by packages/ag2-sparrow/tools/sync_from_src.py.

One acquisition at a time per process: a module lock covers each whole attempt. A fork takes that
lock and the module guard first, so no child is forked mid-acquire or mid-release; the child gets
fresh locks, drops the owner, and closes an inherited lock fd only while it is still the lock file.
The root is held open (owner.root_fd) from before its key is computed for the owner's whole life:
it must still be that directory after the flock and as the last step before Acquired is returned.
After that the owner acts on the root only through that fd, so a path renamed away and replaced is
simply a different root (with its own lock); release closes the fd last. The lock directory is held
open too (owner.dir_fd): after acquire every record read, write, rename and unlink goes through it.
publish's commit is the rename of the record into place: if fsyncing the directory fails after it,
the record is published but its durability is uncertain, so publish returns Published(durable=False)
and logs a warning. Every entry point checks the platform before touching the filesystem.

Threat model: this module defends against other processes, other owners, and stale or copied Owner
objects. It does not defend against code in the owning process that closes, dup2s or reopens this
module's descriptors. One cheap check covers the lock itself: publish and release first try a
non-blocking flock on a fresh open of the lock file, which fails while we hold it; if it succeeds,
the lock is gone and they refuse with LockError("lock lost").
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import math
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

try:
    from . import file_lock
except ImportError:
    import file_lock

try:
    import pwd
except ImportError:  # pragma: no cover - Windows
    pwd = None

SCHEMA = 1
MAX_STR_BYTES = 512
LOCK_NAME = "lock"
HOLDER_NAME = "holder"
WAITING_PREFIX = "waiting."
LEGACY_PIDFILE = os.path.join("state", "connector.pid")
LEGACY_FRESH_S = 30
RECORD_FIELDS = ("schema", "adapter", "pid", "start", "host", "install_id", "lock", "acquired_at")
NETWORK_FS = frozenset(("nfs", "nfs4", "cifs", "smbfs", "smb2", "smb3", "afpfs", "webdav", "davfs",
                        "sshfs", "9p", "ceph", "glusterfs", "lustre", "ncpfs", "fuse", "fuseblk"))
UNSUPPORTED = "unsupported platform: holder_lock is POSIX-only"


class LockError(Exception):
    """A lock operation that cannot proceed; nothing was written."""


# ── results ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Missing:
    pass


@dataclass(frozen=True)
class Valid:
    record: dict


@dataclass(frozen=True)
class Invalid:
    reason: str
    raw_bytes: bytes


Prior = Union[Missing, Valid, Invalid]


@dataclass
class Owner:
    """The in-process handle of this process's one ownership; never passed to a child."""
    fd: int
    lock_id: tuple
    pid: int
    start: str
    root: str
    directory: str
    install_id: str
    acquired_at: float
    root_id: tuple = (0, 0)
    root_fd: int = -1
    dir_fd: int = -1
    dir_id: tuple = (0, 0)
    path: str = ""
    published: bool = False
    released: bool = False
    mutex: Any = field(default_factory=threading.Lock, repr=False)

    @property
    def lock_path(self) -> str:
        return os.path.join(self.directory, LOCK_NAME)

    def __copy__(self):
        raise LockError("an owner cannot be copied")

    def __deepcopy__(self, memo):
        raise LockError("an owner cannot be copied")

    def __reduce_ex__(self, protocol):
        raise LockError("an owner cannot be pickled")


@dataclass(frozen=True)
class Acquired:
    owner: Owner
    prior: Prior


@dataclass(frozen=True)
class Held:
    holder: Prior
    reason: str


@dataclass(frozen=True)
class Contended:
    holder: Prior


@dataclass(frozen=True)
class TimedOut:
    holder: Prior


@dataclass(frozen=True)
class Unreadable:
    reason: str


@dataclass(frozen=True)
class LegacyRunning:
    pid: Optional[int]
    path: str


@dataclass(frozen=True)
class Published:
    record: dict
    durable: bool = True


@dataclass(frozen=True)
class Released:
    record_removed: bool


_OWNER: Optional[Owner] = None
_WAITING: Optional[int] = None
_GUARD = threading.Lock()
_GUARD_THREAD: Optional[int] = None
_ACQUIRING = threading.Lock()
_LOG = logging.getLogger(__name__)


# ── platform, identity, paths ────────────────────────────────────────────────

def _platform() -> None:
    if file_lock.fcntl is None or os.name != "posix" or pwd is None:
        raise LockError(UNSUPPORTED)


def _home() -> str:
    if pwd is None:
        raise LockError(UNSUPPORTED)
    try:
        return pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError:
        raise LockError("no passwd entry for uid %d" % os.geteuid())


def _host() -> str:
    return socket.gethostname()


def lock_dir(root) -> str:
    """The lock directory of an existing work root: keyed by the root directory's (st_dev, st_ino),
    so every spelling of one directory shares it; the effective uid's home places it. The path is
    normalized lexically first, as acquire does."""
    _platform()
    return _lock_dir_of(os.stat(os.path.abspath(os.path.normpath(os.fspath(root)))))


def _lock_dir_of(st) -> str:
    digest = hashlib.sha256(("%d:%d" % (st.st_dev, st.st_ino)).encode("ascii")).hexdigest()[:16]
    return os.path.join(_home(), ".local", "state", "ag2-sparrow", "locks", digest)


def install_id() -> str:
    """The install location only: the distribution and the realpath of this module's directory."""
    dist = "ag2-sparrow" if __package__ else "sutando-src"
    return "%s@%s" % (dist, os.path.realpath(os.path.dirname(os.path.abspath(__file__))))


def same_file(fd: int, path) -> bool:
    """True when `path` (a link is not followed) is the open file `fd`."""
    try:
        held, here = os.fstat(fd), os.lstat(os.fspath(path))
    except (OSError, TypeError, ValueError):
        return False
    return (held.st_dev, held.st_ino) == (here.st_dev, here.st_ino)


def start_time(pid: int) -> Optional[str]:
    """A process's start time as `YYYY-MM-DDTHH:MM:SSZ`, read in the C locale and UTC; None if unseen."""
    env = {"PATH": "/bin:/usr/bin", "LC_ALL": "C", "LANG": "C", "TZ": "UTC"}
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                             env=env, timeout=10).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    text = " ".join(out.split())
    if not text:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def _pid_alive(pid: int) -> Optional[bool]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return None
    return True


def verify_lineage(pid: int, start: Optional[str], host: Optional[str] = None) -> Optional[bool]:
    """True: that very process runs here. False: gone, or the pid now names another process.
    None: it cannot be observed (another host, no start to compare, or an unreadable start time)."""
    if host is not None and host != _host():
        return None
    alive = _pid_alive(pid)
    if alive is not True:
        return alive
    seen = start_time(pid)
    if seen is None or not start:
        return None
    return seen == start


def _statfs_type(path: str) -> Optional[str]:
    """The filesystem type holding `path` (macOS: `nonlocal:<type>` when not MNT_LOCAL), or None."""
    real = os.path.realpath(path)
    best, kind = "", None
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/mounts", encoding="utf-8") as f:
                rows = [line.split()[1:3] for line in f if len(line.split()) >= 3]
        except OSError:
            return None
        for mount, fstype in rows:
            mount = mount.replace("\\040", " ")
            if (real == mount or real.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
                best, kind = mount, fstype
        return kind
    return _darwin_statfs_type(real)


def _darwin_statfs_type(path: str) -> Optional[str]:
    """statfs(2) through libc, so no child process runs before the record exists."""
    import ctypes
    import ctypes.util
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
        fn = getattr(libc, "statfs$INODE64", None) or libc.statfs
    except (OSError, AttributeError):
        return None
    buf = ctypes.create_string_buffer(4096)
    if fn(os.fsencode(path), buf) != 0:
        return None
    flags = int.from_bytes(buf.raw[64:68], sys.byteorder)
    fstype = buf.raw[72:88].split(b"\0", 1)[0].decode("ascii", "replace")
    return fstype if flags & 0x1000 else "nonlocal:" + fstype


def _is_network(fstype: Optional[str]) -> bool:
    return bool(fstype) and (fstype in NETWORK_FS or fstype.startswith(("fuse", "osxfuse", "macfuse", "nonlocal:")))


# ── the record ───────────────────────────────────────────────────────────────

def _short_str(value) -> bool:
    if not isinstance(value, str) or value == "":
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_STR_BYTES
    except UnicodeError:
        return False


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _record_problem(rec) -> Optional[str]:
    """Why `rec` is not a valid schema-1 holder record, or None."""
    if not isinstance(rec, dict):
        return "the record is not a JSON object"
    if not _int(rec.get("schema")) or rec.get("schema") != SCHEMA:
        return "unsupported schema %r" % (rec.get("schema"),)
    missing = [f for f in RECORD_FIELDS if f not in rec]
    if missing:
        return "missing fields %s" % missing
    unknown = set(rec) - set(RECORD_FIELDS)
    if unknown:
        return "unknown fields %s" % sorted(unknown)
    if not (_int(rec["pid"]) and rec["pid"] > 0):
        return "pid is not a positive int"
    lock = rec["lock"]
    if not (isinstance(lock, dict) and set(lock) == {"dev", "ino"} and _int(lock["dev"]) and _int(lock["ino"])):
        return "lock is not {dev: int, ino: int}"
    for name in ("start", "host", "adapter", "install_id"):
        if not _short_str(rec[name]):
            return "%s is not a non-empty string of at most 512 bytes" % name
    if not (isinstance(rec["acquired_at"], float) and math.isfinite(rec["acquired_at"])):
        return "acquired_at is not a finite float"
    return None


def _classify(raw: bytes) -> Prior:
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        return Invalid("not JSON (%s)" % e.__class__.__name__, raw)
    try:
        problem = _record_problem(rec)
    except (UnicodeError, ValueError) as e:
        problem = "not a valid record (%s)" % e.__class__.__name__
    return Invalid(problem, raw) if problem else Valid(rec)


def _read_prior(where) -> Prior:
    """The record in the lock directory `where` (an open dir fd, or a path for readers that hold
    nothing); OSError when it exists but cannot be read. Through a fd, a link is not followed."""
    try:
        if isinstance(where, int):
            fd = os.open(HOLDER_NAME, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=where)
            with os.fdopen(fd, "rb") as f:
                raw = f.read()
        else:
            with open(os.path.join(where, HOLDER_NAME), "rb") as f:
                raw = f.read()
    except FileNotFoundError:
        return Missing()
    return _classify(raw)


def _holder_of(directory) -> Prior:
    """The record in a known lock directory, typed: an unreadable one is Invalid."""
    try:
        return _read_prior(directory)
    except OSError as e:
        return Invalid("unreadable: %s" % e, b"")
    except (UnicodeError, ValueError) as e:
        return Invalid("not a valid record (%s)" % e.__class__.__name__, b"")


def read_holder(work_root) -> Prior:
    """Missing, Valid or Invalid. Never raises on a supported platform: an unreadable record is Invalid;
    a root that does not exist has no record."""
    try:
        directory = lock_dir(work_root)
    except FileNotFoundError:
        return Missing()
    except OSError as e:
        return Invalid("unreadable: %s" % e, b"")
    return _holder_of(directory)


def _pids_in(raw: bytes):
    """(pid, start) pairs an invalid record still names, parsed as far as it can be."""
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return [(int(p), None) for p in re.findall(rb'"pid"\s*:\s*(\d{1,10})', raw) if int(p) > 0]
    if isinstance(rec, dict) and _int(rec.get("pid")) and rec["pid"] > 0:
        return [(rec["pid"], rec.get("start") if isinstance(rec.get("start"), str) else None)]
    return []


def _prior_refusal(prior: Prior) -> Optional[str]:
    """The Held reason a prior gives, or None when it names no live or unobserved process."""
    if isinstance(prior, Valid):
        seen = verify_lineage(prior.record["pid"], prior.record["start"], prior.record["host"])
        return "live holder" if seen is True else "unobserved" if seen is None else None
    if isinstance(prior, Invalid):
        for pid, start in _pids_in(prior.raw_bytes):
            if verify_lineage(pid, start) is not False:
                return "invalid record naming a live or unobserved pid"
    return None


def _legacy_finding(root: str, root_fd: Optional[int] = None):
    """(LegacyRunning or None, stale) from the one read of a pre-holder_lock adapter's pidfile, read
    through the held root when given. An empty or malformed file refuses only while fresh; older, it
    is a crash leftover (stale)."""
    path = os.path.join(root, LEGACY_PIDFILE)
    try:
        fd = os.open(LEGACY_PIDFILE if root_fd is not None else path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                     dir_fd=root_fd)
        with os.fdopen(fd, "rb") as f:
            data = f.read()
            mtime = os.fstat(f.fileno()).st_mtime
            age = time.time() - mtime
    except FileNotFoundError:
        return None, False
    except OSError:
        return LegacyRunning(None, path), False
    m = re.fullmatch(rb"\s*([1-9][0-9]{0,9})\s*", data)
    if not m:
        return (LegacyRunning(None, path), False) if age < LEGACY_FRESH_S else (None, True)
    pid = int(m.group(1))
    if verify_lineage(pid, None) is False or _started_after(pid, mtime):
        return None, False
    return LegacyRunning(pid, path), False


def _started_after(pid: int, mtime: float) -> bool:
    """The pid's current process started after the file was written: a reused pid, not its writer."""
    start = start_time(pid)
    if start is None:
        return False
    return calendar.timegm(time.strptime(start, "%Y-%m-%dT%H:%M:%SZ")) > mtime


# ── acquire, wait ────────────────────────────────────────────────────────────

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC


def _prepare(work_root):
    """Platform and owner checks, the normalized root and the lock dir created descriptor-relative.
    Returns (path, root, directory, root_fd, dir_fd); the caller closes both fds. Runs under
    _ACQUIRING."""
    _platform()
    with _guarded:
        if _OWNER is not None and not _OWNER.released and _OWNER.pid == os.getpid():
            raise LockError("already owner")
    path = os.path.abspath(os.path.normpath(os.fspath(work_root)))
    root_fd = _open_dirs(os.sep, [p for p in path.split(os.sep) if p], 0o777)
    root = os.path.realpath(path)
    try:
        held = os.fstat(root_fd)
        directory = _lock_dir_of(held)
        home = _home()
        parts = os.path.relpath(directory, home).split(os.sep)
        dir_fd = _open_dirs(home, parts, 0o700, inside=(held.st_dev, held.st_ino))
    except BaseException:
        os.close(root_fd)
        raise
    try:
        if _is_network(_statfs_type(directory)):
            raise LockError("not a local filesystem")
    except BaseException:
        os.close(dir_fd)
        os.close(root_fd)
        raise
    return path, root, directory, root_fd, dir_fd


def _open_dirs(start: str, parts, mode: int, inside=None) -> int:
    """Open `start`, then each of `parts` from the one above (creating it with `mode` if absent), and
    return the last fd. With `inside` (a root's (st_dev, st_ino)), every opened directory is checked
    by its physical ancestry first, so nothing is ever created under that root."""
    fd = os.open(start, _DIR_FLAGS)
    try:
        if inside is not None and _fd_within(fd, inside):
            raise LockError("work root contains the lock directory")
        for part in parts:
            made = False
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if inside is not None and _fd_within(fd, inside):
                    raise LockError("work root contains the lock directory")
                try:
                    os.mkdir(part, mode, dir_fd=fd)
                    made = True
                except FileExistsError:
                    pass
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            if inside is not None and _fd_within(nxt, inside):
                os.close(nxt)
                if made:
                    with _suppress_os_error():
                        os.rmdir(part, dir_fd=fd)
                raise LockError("work root contains the lock directory")
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


class _suppress_os_error:
    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        return kind is not None and issubclass(kind, OSError)


def _same_at(fd: int, dir_fd: int, name: str) -> bool:
    """True when `name` in the directory open at `dir_fd` (a link is not followed) is the open file `fd`."""
    try:
        held, here = os.fstat(fd), os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except (OSError, TypeError, ValueError):
        return False
    return (held.st_dev, held.st_ino) == (here.st_dev, here.st_ino)


def _dir_unchanged(directory: str, dir_fd: int) -> bool:
    """The lock directory's path (a link is not followed) still names the directory held at `dir_fd`."""
    try:
        here, held = os.lstat(directory), os.fstat(dir_fd)
    except OSError:
        return False
    return stat.S_ISDIR(here.st_mode) and (here.st_dev, here.st_ino) == (held.st_dev, held.st_ino)


def _fd_within(fd: int, root_id) -> bool:
    """Whether the directory open at `fd` is the root or lies under it, by walking ".." from it."""
    cur = os.dup(fd)
    try:
        while True:
            st = os.fstat(cur)
            if (st.st_dev, st.st_ino) == tuple(root_id):
                return True
            parent = os.open("..", _DIR_FLAGS, dir_fd=cur)
            os.close(cur)
            cur = parent
            up = os.fstat(cur)
            if (up.st_dev, up.st_ino) == (st.st_dev, st.st_ino):
                return False
    finally:
        os.close(cur)


def _root_unchanged(path: str, root: str, root_fd: int) -> bool:
    """The path still names the directory held open since before its key was computed."""
    try:
        st, held = os.lstat(path), os.fstat(root_fd)
        now = os.stat(root)
    except OSError:
        return False
    return (stat.S_ISDIR(st.st_mode) and os.path.realpath(path) == root
            and (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino))


_FORK_HOOK = []


def _register_fork_hooks() -> None:
    """At import, once: a fork waits for any acquisition or release in progress, and the child
    starts with fresh locks and no owner."""
    if not _FORK_HOOK and hasattr(os, "register_at_fork"):
        os.register_at_fork(before=_before_fork, after_in_parent=_after_fork_in_parent,
                            after_in_child=_drop_in_child)
        _FORK_HOOK.append(True)


def _before_fork() -> None:
    _ACQUIRING.acquire()
    _GUARD.acquire()


def _after_fork_in_parent() -> None:
    _GUARD.release()
    _ACQUIRING.release()


def _drop_in_child() -> None:
    global _OWNER, _WAITING, _GUARD, _ACQUIRING, _GUARD_THREAD
    _GUARD, _ACQUIRING, _GUARD_THREAD = threading.Lock(), threading.Lock(), None
    owner, _OWNER, _WAITING = _OWNER, None, None
    if owner is None:
        return
    for fd, want in ((owner.fd, tuple(owner.lock_id)), (owner.dir_fd, tuple(owner.dir_id)),
                     (owner.root_fd, tuple(owner.root_id))):
        try:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) == want:
                os.close(fd)
        except OSError:
            pass


_register_fork_hooks()


def _attempt(path: str, root: str, directory: str, root_fd: int, dir_fd: int, install_id_: str):
    global _OWNER
    made = False
    try:
        fd = os.open(LOCK_NAME, os.O_RDWR | _NOFOLLOW | _CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        try:
            fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dir_fd)
            made = True
        except FileExistsError:
            fd = os.open(LOCK_NAME, os.O_RDWR | _NOFOLLOW | _CLOEXEC, dir_fd=dir_fd)
    held = os.fstat(root_fd)
    if _fd_within(dir_fd, (held.st_dev, held.st_ino)):
        os.close(fd)
        if made:
            with _suppress_os_error():
                os.unlink(LOCK_NAME, dir_fd=dir_fd)
        raise LockError("work root contains the lock directory")
    locked = kept = False
    try:
        if not file_lock.try_lock_fd(fd):
            return Contended(_holder_of(dir_fd))
        locked = True
        if not (_same_at(fd, dir_fd, LOCK_NAME) and _dir_unchanged(directory, dir_fd)):
            raise LockError("replaced")
        if not _root_unchanged(path, root, root_fd):
            raise LockError("root changed")
        try:
            prior = _read_prior(dir_fd)
        except OSError as e:
            return Unreadable(str(e))
        except (UnicodeError, ValueError) as e:
            prior = Invalid("not a valid record (%s)" % e.__class__.__name__, b"")
        reason = _prior_refusal(prior)
        if reason:
            return Held(prior, reason=reason)
        legacy, _ = _legacy_finding(root, root_fd)
        if legacy:
            return legacy
        start = start_time(os.getpid())
        if start is None:
            raise LockError("cannot read this process's start time")
        st, held, dst = os.fstat(fd), os.fstat(root_fd), os.fstat(dir_fd)
        owner = Owner(fd=fd, lock_id=(st.st_dev, st.st_ino), pid=os.getpid(), start=start, root=root,
                      directory=directory, install_id=install_id_, acquired_at=time.time(),
                      root_id=(held.st_dev, held.st_ino), root_fd=root_fd, dir_fd=dir_fd,
                      dir_id=(dst.st_dev, dst.st_ino), path=path)
        with _guarded:
            if not _root_unchanged(path, root, root_fd):
                raise LockError("root changed")
            _OWNER = owner
            kept = True
        return Acquired(owner, prior)
    finally:
        if not kept:
            try:
                if locked:
                    file_lock.unlock_fd(fd)
            finally:
                os.close(fd)


def _one_attempt(work_root, install_id_: str):
    with _ACQUIRING:
        path, root, directory, root_fd, dir_fd = _prepare(work_root)
        result = None
        try:
            result = _attempt(path, root, directory, root_fd, dir_fd, install_id_)
            return result
        finally:
            if not isinstance(result, Acquired):
                os.close(dir_fd)
                os.close(root_fd)


def acquire(work_root, *, install_id: str, timeout: float = 0.0):
    """Acquired | Held | Contended | TimedOut | Unreadable | LegacyRunning (TimedOut only with a timeout)."""
    if timeout and timeout > 0:
        return wait(work_root, install_id=install_id, deadline=time.monotonic() + timeout)
    return _one_attempt(work_root, install_id)


def _write_waiting(dir_fd: int, start: str) -> str:
    """This process's waiter file, written through the held lock-directory fd: a fresh temp
    (O_EXCL, no link followed), fsynced, renamed into place. Returns its name."""
    name = "%s%d" % (WAITING_PREFIX, os.getpid())
    tmp = ".%s%d.%s.tmp" % (WAITING_PREFIX, os.getpid(), uuid.uuid4().hex[:8])
    data = ("%s %.3f\n" % (start, time.time())).encode("ascii", "replace")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        try:
            while data:
                data = data[os.write(fd, data):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with _suppress_os_error():
            os.unlink(tmp, dir_fd=dir_fd)
        raise
    return name


def wait(work_root, *, install_id: str, deadline: float, poll: float = 0.1,
         on_tick: Optional[Callable[[float, Prior], None]] = None):
    """Acquired | TimedOut | Unreadable | LegacyRunning, polling until `deadline` (time.monotonic()).
    The verified lock directory stays open for the whole wait; the waiter file is written and removed
    only through it, and each attempt first checks the directory's path still names it."""
    global _WAITING
    with _ACQUIRING:
        _, _, directory, root_fd, dir_fd = _prepare(work_root)
        os.close(root_fd)
    try:
        with _guarded:
            if _WAITING == os.getpid():
                raise LockError("already waiting")
            _WAITING = os.getpid()
    except BaseException:
        os.close(dir_fd)
        raise
    mine = None
    try:
        mine = _write_waiting(dir_fd, start_time(os.getpid()) or "unknown")
        began = time.monotonic()
        while True:
            if not _dir_unchanged(directory, dir_fd):
                raise LockError("replaced")
            result = _one_attempt(work_root, install_id)
            if not isinstance(result, (Held, Contended)):
                return result
            if time.monotonic() >= deadline:
                return TimedOut(result.holder)
            if on_tick is not None:
                on_tick(time.monotonic() - began, result.holder)
            time.sleep(max(0.0, min(poll, deadline - time.monotonic())))
    finally:
        try:
            if mine:
                try:
                    os.unlink(mine, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(dir_fd)
            with _guarded:
                _WAITING = None


# ── publish, release ─────────────────────────────────────────────────────────

class _Guarded:
    """_GUARD for a section of this module: a same-thread re-entry (a signal handler calling in while
    its thread is inside) raises LockError("re-entrant") instead of blocking forever."""

    def __enter__(self):
        global _GUARD_THREAD
        if _GUARD_THREAD == threading.get_ident():
            raise LockError("re-entrant")
        _GUARD.acquire()
        _GUARD_THREAD = threading.get_ident()
        return self

    def __exit__(self, *exc):
        global _GUARD_THREAD
        _GUARD_THREAD = None
        _GUARD.release()
        return False


_guarded = _Guarded()


def _check_owner(owner: Owner) -> None:
    """Outside any lock: the platform, and the owner pid (a forked child stops here)."""
    _platform()
    if owner.pid != os.getpid():
        raise LockError("not the owner")


def _held_fds(owner: Owner) -> None:
    """Under _GUARD: the owner is this process's current owner object (never a copy or a stale handle),
    unreleased, and its fds are still the lock file and the held root; else LockError before any fd
    is touched."""
    if owner.released or owner is not _OWNER or owner.root_fd < 0 or owner.dir_fd < 0:
        raise LockError("released")
    for fd, want in ((owner.fd, tuple(owner.lock_id)), (owner.dir_fd, tuple(owner.dir_id)),
                     (owner.root_fd, tuple(owner.root_id))):
        try:
            st = os.fstat(fd)
        except OSError:
            raise LockError("released")
        if (st.st_dev, st.st_ino) != want:
            raise LockError("released")


def _lock_still_held(owner: Owner) -> None:
    """Our open file description still holds the flock. A fresh open of the lock file must fail a
    non-blocking flock (somebody holds it), and re-asserting it on owner.fd must succeed (that is us)."""
    try:
        fd = os.open(LOCK_NAME, os.O_RDWR | _NOFOLLOW | _CLOEXEC, dir_fd=owner.dir_fd)
    except OSError as e:
        raise LockError("lock lost: %s" % e)
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != tuple(owner.lock_id):
            raise LockError("replaced")
        if file_lock.try_lock_fd(fd):
            file_lock.unlock_fd(fd)
            raise LockError("lock lost")
    finally:
        os.close(fd)
    if not file_lock.try_lock_fd(owner.fd):
        raise LockError("lock lost")


def _write_record(owner: Owner, rec: dict) -> bool:
    """Write the record; the rename is the commit. True when the directory fsync after it also
    succeeded; LockError when anything before the rename failed: no record is published, though a
    temp whose cleanup also failed may remain; each later publish tries to remove it (logged if not)."""
    dfd = owner.dir_fd
    try:
        names = os.listdir(dfd)
    except OSError as e:
        raise LockError("cannot write the holder record: %s" % e)
    for name in names:
        if name.startswith("." + HOLDER_NAME + ".") and name.endswith(".tmp"):
            try:
                os.unlink(name, dir_fd=dfd)
            except OSError as e:
                _LOG.warning("a leftover holder temp %s could not be removed (%s); it stays", name, e)
    tmp = ".%s.%d-%d.tmp" % (HOLDER_NAME, owner.lock_id[0], owner.lock_id[1])
    data = json.dumps(rec, sort_keys=True, ensure_ascii=True).encode("ascii")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dfd)
        try:
            while data:
                data = data[os.write(fd, data):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, HOLDER_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
    except OSError as e:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise LockError("cannot write the holder record: %s" % e)
    try:
        os.fsync(dfd)
    except OSError as e:
        _LOG.warning("holder record published in %s, but its directory fsync failed (%s): durability "
                     "is uncertain", owner.directory, e)
        return False
    return True


def publish(owner: Owner, adapter: str) -> Published:
    """Write the holder record, once per owner; every field but `adapter` is generated. Validated
    and written under the module guard, so a released handle never writes."""
    _check_owner(owner)
    with _guarded:
        _held_fds(owner)
        if owner.published:
            raise LockError("published")
        if not (_same_at(owner.fd, owner.dir_fd, LOCK_NAME) and _dir_unchanged(owner.directory, owner.dir_fd)):
            raise LockError("replaced")
        _lock_still_held(owner)
        rec = {"schema": SCHEMA, "adapter": adapter, "pid": owner.pid, "start": owner.start, "host": _host(),
               "install_id": owner.install_id, "lock": {"dev": owner.lock_id[0], "ino": owner.lock_id[1]},
               "acquired_at": float(owner.acquired_at)}
        problem = _record_problem(rec)
        if problem:
            raise LockError("invalid record: %s" % problem)
        durable = _write_record(owner, rec)
        owner.published = True
        return Published(dict(rec, lock=dict(rec["lock"])), durable=durable)


def release(owner: Owner) -> Released:
    """Under the module guard: validate the handle, remove the record only if it names this owner,
    unlock and close (the root fd last), and only then mark released. Owner pid only."""
    global _OWNER
    _check_owner(owner)
    with _guarded:
        _held_fds(owner)
        if _same_at(owner.fd, owner.dir_fd, LOCK_NAME):
            _lock_still_held(owner)
        removed = False
        try:
            if _same_at(owner.fd, owner.dir_fd, LOCK_NAME):
                prior = _read_prior(owner.dir_fd)
                if isinstance(prior, Valid) and prior.record["pid"] == owner.pid \
                        and prior.record["start"] == owner.start \
                        and (prior.record["lock"]["dev"], prior.record["lock"]["ino"]) == tuple(owner.lock_id):
                    os.unlink(HOLDER_NAME, dir_fd=owner.dir_fd)
                    removed = True
        except OSError:
            removed = False
        finally:
            try:
                file_lock.unlock_fd(owner.fd)
            finally:
                os.close(owner.fd)
                os.close(owner.dir_fd)
                if owner.root_fd >= 0:
                    os.close(owner.root_fd)
                owner.released = True
                if _OWNER is owner:
                    _OWNER = None
        return Released(removed)


# ── status ───────────────────────────────────────────────────────────────────

def _waiter_live(directory: str, pid: int) -> bool:
    try:
        with open(os.path.join(directory, "%s%d" % (WAITING_PREFIX, pid)), encoding="utf-8") as f:
            start = f.read().split(" ", 1)[0]
    except OSError:
        return False
    return verify_lineage(pid, start) is True


def status(work_root) -> dict:
    """The record, its holder's liveness, live and stale waiters, and any legacy pidfile. Read-only."""
    _platform()
    root = os.path.realpath(os.fspath(work_root))
    try:
        directory = lock_dir(root)
    except FileNotFoundError:
        return {"lock_dir": None, "record": {"kind": "missing"}, "holder_lineage": None, "waiters": [],
                "stale_waiters": [], "legacy": None, "warnings": []}
    out = {"lock_dir": directory, "record": None, "holder_lineage": None, "waiters": [], "stale_waiters": [],
           "legacy": None, "warnings": []}
    try:
        prior = _read_prior(directory)
    except OSError as e:
        out["record"] = {"kind": "unreadable", "reason": str(e)}
        prior = None
    if isinstance(prior, Missing):
        out["record"] = {"kind": "missing"}
    elif isinstance(prior, Invalid):
        out["record"] = {"kind": "invalid", "reason": prior.reason}
    elif isinstance(prior, Valid):
        out["record"] = {"kind": "valid", "record": prior.record}
        out["holder_lineage"] = verify_lineage(prior.record["pid"], prior.record["start"], prior.record["host"])
    owner = _OWNER
    if owner is not None and not owner.released and owner.pid == os.getpid() and owner.directory == directory:
        out["root_unchanged"] = _root_unchanged(owner.path or owner.root, owner.root, owner.root_fd)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        names = []
    for name in names:
        if name.startswith(WAITING_PREFIX) and name[len(WAITING_PREFIX):].isdigit():
            pid = int(name[len(WAITING_PREFIX):])
            out["waiters" if _waiter_live(directory, pid) else "stale_waiters"].append(pid)
    legacy, stale = _legacy_finding(root)
    path = os.path.join(root, LEGACY_PIDFILE)
    if legacy:
        out["legacy"] = {"path": path, "pid": legacy.pid, "running": True}
    elif os.path.lexists(path):
        out["legacy"] = {"path": path, "pid": None, "running": False, "stale": stale}
    if stale:
        out["warnings"].append("%s is empty or malformed and older than %ds: a crash leftover, ignored; "
                               "remove it" % (path, LEGACY_FRESH_S))
    return out
