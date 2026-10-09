#!/usr/bin/env python3
"""Terminal disposal of a result file: claim the generation read, verify it,
quarantine it once; recover the claims a crashed owner left behind.

A terminal outbox decision moves its live `results/<task-id>.txt` aside exactly
once. Several observers (the outbound drain, the orphan sweep, a restarted
process) can reach the same decision, and a producer can publish a newer reply
at the same name at any moment, so disposal is bound to the exact file an
observer read (`ResultIdentity`: inode, write time and bytes), never to the
pathname.

Observers are serialized, not reasoned about: every transition and every
recovery pass runs under one exclusive lock per results directory
(`results/.disposal.lock`, which must never be removed while in use). What the
lock cannot cover is the producer, which writes results without it, so every
step that touches the canonical name is a conditional primitive: a put-back is
an atomic no-replace rename (`renameat2(RENAME_NOREPLACE)` on Linux,
`renamex_np(RENAME_EXCL)` on macOS), so a claim never has a second name that
could be unlinked after a producer retook the canonical one. Where neither
primitive exists the fallback links and verifies, and says so.

The move is two renames through a private claim,
`results/.<stem>.disposing-<pid>-<birth-usec>-<acquired-s>-<ino>-<mtime-ns>-<sha256>-<nonce>[.restore]`.
The name carries the owner's birth token (a recycled pid is not an owner), the
acquisition time (a rename keeps the result's own mtime, which says when the
reply was written, not when it was claimed) and the full identity of the
generation the owner meant to dispose of, so a recovery pass knows whether a
stranded body is that very publication (quarantine it) or a reply the owner
never verified (put it back live). `.restore` records that the owner was
putting one back. A death between the renames leaves a body at the claim
path; every pass recovers the claims whose owner cannot finish, so no reply
is stranded where the operator cannot see it, one damaged claim never blocks
the others, and a pass that cannot take the lock never blocks delivery.

Residual: on a filesystem with coarse timestamps an inode reused within one
tick for equal bytes still reads as the same publication. The content
survives either way; only the loser's report can be wrong.

Dependency-light: the caller supplies its results directory and a log callback.
"""
from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import hashlib
import os
import re
import stat as _stat
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, Optional

try:
    from .result_ready import ResultIdentity, identity_of
except ImportError:  # pragma: no cover - whichever import wins is exercised
    try:
        from .readiness import ResultIdentity, identity_of
    except ImportError:
        from delivery.readiness import ResultIdentity, identity_of
try:
    from .outbox import OwnerState, process_identity
except ImportError:  # pragma: no cover
    from outbox import OwnerState, process_identity
try:
    from . import undelivered_quarantine
except ImportError:  # pragma: no cover
    import undelivered_quarantine
try:
    from .file_lock import lock_fd, unlock_fd
except ImportError:  # pragma: no cover
    from file_lock import lock_fd, unlock_fd

__all__ = ["CLAIM_MAX_S", "LOCK_WAIT_S", "LOCK_NAME", "ACTIVE_CLAIMS", "RENAME_PRIMITIVE",
           "Claim", "GenerationReplaced", "DisposalBusy", "locked", "rename_noreplace",
           "self_token", "parse_claim", "find_claims", "find_malformed", "owner_holds",
           "quarantine_generation", "put_back", "recover_claim", "recover_abandoned_claims",
           "disposed_copy_exists"]

# Only an owner whose liveness cannot be read (EPERM) ages out: the bound is a
# fallback for UNKNOWN, never a reason to take a claim from a live process.
CLAIM_MAX_S = 600.0
# A stamp this far ahead of the clock was not written by a sane clock.
FUTURE_SLACK_S = 60.0
# A pass that cannot take the lock in this time skips its work rather than guessing.
LOCK_WAIT_S = 30.0
LOCK_NAME = ".disposal.lock"
# Claims THIS process is moving right now; one it made but no longer holds was abandoned.
ACTIVE_CLAIMS: "set[str]" = set()
# Reported once per process: a claim whose recovery failed, a malformed name, a busy lock.
_REPORTED: "set[str]" = set()
_HELD = threading.local()
_BUSY_ERRNOS = {errno.EAGAIN, errno.EACCES, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)}

_NAME = ".{stem}.disposing-{pid}-{start}-{acquired}-{ino}-{mtime}-{digest}-{nonce}"
_RESTORE = ".restore"
_RE = re.compile(r"^\.(?P<stem>task-.+)\.disposing-(?P<pid>\d+)-(?P<start>\d+)-"
                 r"(?P<acquired>\d+)-(?P<ino>\d+)-(?P<mtime>\d+)-(?P<digest>[0-9a-f]{64})-"
                 r"(?P<nonce>[0-9a-f]+)(?P<intent>\.restore)?$")

Log = Callable[[str], None]


class Claim(NamedTuple):
    path: Path
    stem: str
    pid: int
    start: int
    acquired: int
    ino: int
    mtime_ns: int
    digest: str
    restore: bool


class GenerationReplaced(FileNotFoundError):
    """The file this observer read is no longer at its name: a newer reply is
    (or was put back) there, and it stays live."""


class DisposalBusy(OSError):
    """The results directory's disposal lock stayed held for the whole wait."""


# ── the no-replace rename ────────────────────────────────────────────────────

def _probe_rename() -> "tuple[str, Optional[Callable[[bytes, bytes], int]]]":
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    except OSError:  # pragma: no cover - no libc to speak of
        return "link", None
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):  # pragma: no cover - Linux CI
        fn = libc.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        return "renamex_np", lambda a, b: fn(a, b, 4)                    # RENAME_EXCL
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        at_fdcwd = -100
        return "renameat2", lambda a, b: fn(at_fdcwd, a, at_fdcwd, b, 1)  # RENAME_NOREPLACE
    return "link", None


RENAME_PRIMITIVE, _RENAME = _probe_rename()


def rename_noreplace(src: Path, dst: Path, log: Optional[Log] = None) -> None:
    """Rename `src` to `dst` only if `dst` does not exist: FileExistsError
    otherwise, and nothing moved. Without a kernel primitive the fallback
    links, verifies the link still names the same file, then unlinks: a name
    retaken between link and verify is reported; one retaken between the
    verify and the unlink is a residual silent loss on such platforms."""
    global RENAME_PRIMITIVE, _RENAME
    if _RENAME is not None:
        rc = _RENAME(os.fsencode(src), os.fsencode(dst))
        if rc == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(dst))
        if err in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
            RENAME_PRIMITIVE, _RENAME = "link", None   # this filesystem cannot; fall through
        else:
            raise OSError(err, os.strerror(err), str(src))
    _report_once("rename:fallback", log or (lambda _m: None),
                 "result disposal: no no-replace rename on this platform; put-back links then "
                 "unlinks, so a producer retaking the name in that gap is reported, not silent")
    os.link(src, dst)                                   # FileExistsError: name taken
    try:
        sa, sb = os.stat(src), os.stat(dst)
    except FileNotFoundError:
        return
    if (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino):
        os.unlink(src)
        return
    # A producer replaced the canonical name between link and verify: `src`
    # is the restored reply's last link, so it stays, and the caller is told.
    raise FileExistsError(errno.EEXIST, "canonical name retaken during the put-back", str(dst))


# ── the lock ─────────────────────────────────────────────────────────────────

def _open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o644)
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, f"{path.name} is not a regular file", str(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _try_lock(fd: int) -> bool:
    try:
        lock_fd(fd, blocking=False)
        return True
    except BlockingIOError:
        return False
    except OSError as e:
        if e.errno in _BUSY_ERRNOS:
            return False
        raise


@contextlib.contextmanager
def locked(results_dir: Path) -> Iterator[None]:
    """One exclusive lock per results directory, re-entrant per directory
    within a thread. Two threads of one process lock two open file
    descriptions, so the drain and the sweep serialize exactly like two
    processes do. After locking, the fd must still be the file at the lock's
    name, which only protects a locker arriving after a replacement; the
    contract is that nothing removes the lock file while it is in use."""
    results_dir = Path(results_dir)
    key = os.path.realpath(results_dir)
    held = getattr(_HELD, "dirs", None)
    if held is None:
        held = _HELD.dirs = {}
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    path = results_dir / LOCK_NAME
    deadline = time.monotonic() + LOCK_WAIT_S
    for _attempt in range(8):
        fd = _open_lock(path)
        try:
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise DisposalBusy(f"{LOCK_NAME} held for more than {LOCK_WAIT_S:.0f}s")
                time.sleep(0.02)
            try:
                here = os.stat(path)
            except FileNotFoundError:
                here = None
            mine = os.fstat(fd)
            if here is None or (here.st_dev, here.st_ino) != (mine.st_dev, mine.st_ino):
                unlock_fd(fd)
                continue                                # the lock file was replaced: lock the new one
            held[key] = 1
            try:
                yield
            finally:
                held[key] = 0
                unlock_fd(fd)
            return
        finally:
            os.close(fd)
    raise OSError(errno.ESTALE, f"{LOCK_NAME} keeps being replaced", str(path))


# ── claims ───────────────────────────────────────────────────────────────────

def self_token() -> "tuple[int, int]":
    ident = process_identity(os.getpid())
    return os.getpid(), int(ident.start_usec or 0)


def parse_claim(path: Path) -> Optional[Claim]:
    m = _RE.match(Path(path).name)
    if not m:
        return None
    return Claim(Path(path), m.group("stem"), int(m.group("pid")), int(m.group("start")),
                 int(m.group("acquired")), int(m.group("ino")), int(m.group("mtime")),
                 m.group("digest"), bool(m.group("intent")))


def find_claims(results_dir: Path, stem: Optional[str] = None) -> "list[Path]":
    pattern = f".{stem}.disposing-*" if stem else ".task-*.disposing-*"
    try:
        return sorted(p for p in Path(results_dir).glob(pattern) if parse_claim(p))
    except OSError:
        return []


def find_malformed(results_dir: Path) -> "list[Path]":
    """Claim-shaped names this module cannot parse: never moved, named once."""
    try:
        return sorted(p for p in Path(results_dir).glob(".task-*.disposing-*") if not parse_claim(p))
    except OSError:
        return []


def owner_holds(claim: Path, now: Optional[float] = None) -> bool:
    """True while the claim's owner may still finish the move. Liveness first:
    a dead or recycled pid never holds, a live owner always does (its moves run
    under the lock, so a claim it still has is one it is still making), and
    only an owner whose state cannot be read ages out. Our own pid holds only
    what it is moving."""
    c = parse_claim(claim)
    if c is None:
        return True                                     # unknown shape: never touch
    if c.pid == os.getpid():
        return str(claim) in ACTIVE_CLAIMS
    owner = process_identity(c.pid)
    if owner.state is OwnerState.DEAD:
        return False
    if owner.state is OwnerState.ALIVE:
        return not (c.start and owner.start_usec and c.start != owner.start_usec)
    now = now if now is not None else time.time()
    if c.acquired > now + FUTURE_SLACK_S:
        return False                                    # a stamp from the future is stale
    return now - c.acquired <= CLAIM_MAX_S


def _same_file(a: Path, b: Path) -> bool:
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _is_generation(path: Path, generation: ResultIdentity) -> bool:
    """`path` is the very publication `generation` describes: same inode,
    write time and bytes. Missing is False; any other failure to read is the
    caller's to report, not a verdict."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    if (st.st_dev, st.st_ino, st.st_mtime_ns) != (generation.dev, generation.ino, generation.mtime_ns):
        return False
    return identity_of(path)[1] == generation


def _quarantine_target(results_dir: Path, stem: str) -> Path:
    d = undelivered_quarantine.quarantine_dir(results_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d / undelivered_quarantine.quarantine_name(stem)


def _drop_link(claim: Path, log: Log, stem: str, results_dir: Path) -> None:
    """Remove a claim that is a second name of a restored or quarantined file
    (only the link fallback can leave one). While it is the sole link it is a
    reply's last copy and is kept where the operator looks instead."""
    try:
        if os.stat(claim).st_nlink > 1:
            os.unlink(claim)
            return
    except FileNotFoundError:
        return
    kept = _quarantine_target(results_dir, stem)
    os.rename(claim, kept)
    log(f"result {stem}: a superseded reply was kept as {kept.name}")


def quarantine_generation(results_dir: Path, rfile: Path, generation: ResultIdentity,
                          log: Log) -> Path:
    """Quarantine `rfile` only if it still IS `generation`. The rename to a
    private claim is the atomic step; a different file found there is a newer
    reply and goes back under a recorded `.restore` intent. A failure after
    the claim rename puts the body back or quarantines it: the claim path is
    never where a result ends up."""
    with locked(results_dir):
        return _quarantine_generation(Path(results_dir), Path(rfile), generation, log)


def _verify_fd(fd: int, generation: ResultIdentity) -> "tuple[bool, os.stat_result]":
    """The open file is `generation`: identity before and after hashing the
    same descriptor, so a producer rewriting the inode in place is caught."""
    st = os.fstat(fd)
    if (st.st_dev, st.st_ino, st.st_mtime_ns) != (generation.dev, generation.ino, generation.mtime_ns):
        return False, st
    h = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            break
        h.update(chunk)
    again = os.fstat(fd)
    same = (h.hexdigest() == generation.digest
            and (again.st_mtime_ns, again.st_size) == (st.st_mtime_ns, st.st_size))
    return same, again


def _quarantine_generation(results_dir: Path, rfile: Path, generation: ResultIdentity,
                           log: Log) -> Path:
    pid, start = self_token()
    claim = rfile.with_name(_NAME.format(stem=rfile.stem, pid=pid, start=start,
                                         acquired=int(time.time()), ino=generation.ino,
                                         mtime=generation.mtime_ns, digest=generation.digest,
                                         nonce=uuid.uuid4().hex[:8]))
    ACTIVE_CLAIMS.add(str(claim))
    try:
        os.rename(rfile, claim)                       # FileNotFoundError: nothing there
        try:
            fd = os.open(claim, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                matches, before = _verify_fd(fd, generation)
                if matches:
                    target = _quarantine_target(results_dir, rfile.stem)
                    os.rename(claim, target)
                    after = os.fstat(fd)
                    if (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size):
                        log(f"result {rfile.stem}: the quarantined file was rewritten in place "
                            f"during the move; {target.name} is not the verified bytes")
                    return target
            finally:
                os.close(fd)
        except OSError:
            if put_back(claim, rfile, log):
                raise
            return _recover_claim(results_dir, claim, log, quiet=True) or rfile
        restoring = claim.with_name(claim.name + _RESTORE)
        ACTIVE_CLAIMS.add(str(restoring))             # registered before it can be seen
        try:
            os.rename(claim, restoring)               # the intent survives a crash
            if put_back(restoring, rfile, log):
                raise GenerationReplaced(str(rfile))
            # Yet another reply landed meanwhile; the one we hold is superseded but
            # is still someone's answer, so it is kept where the operator looks.
            kept = _quarantine_target(results_dir, rfile.stem)
            os.rename(restoring, kept)
            log(f"result {rfile.stem}: a superseded reply was kept as {kept.name}")
            raise GenerationReplaced(str(rfile))
        finally:
            ACTIVE_CLAIMS.discard(str(restoring))
    finally:
        ACTIVE_CLAIMS.discard(str(claim))


def put_back(claim: Path, rfile: Path, log: Optional[Log] = None) -> bool:
    """Return the claimed body to its canonical name with one no-replace
    rename: True when it is back, False when that name is taken again (the
    claim then still holds the body, untouched). Nothing is ever unlinked
    here, so a producer retaking the name cannot cost the reply its last link."""
    try:
        rename_noreplace(Path(claim), Path(rfile), log)
    except FileExistsError:
        return False
    return True


def recover_claim(results_dir: Path, claim: Path, log: Log, quiet: bool = False) -> Optional[Path]:
    """Finish what an abandoned claim's owner could not: a body the owner
    never verified (its identity is not the one named in the claim) or a
    `.restore` claim goes back to its canonical name; the generation the
    owner meant to dispose of goes where the operator looks. A claim that is
    merely a second name of a file already restored or quarantined is
    dropped. Returns the path the body ended at, None when nothing was left."""
    with locked(results_dir):
        return _recover_claim(Path(results_dir), Path(claim), log, quiet)


def _recover_claim(results_dir: Path, claim: Path, log: Log, quiet: bool = False) -> Optional[Path]:
    c = parse_claim(claim)
    if c is None:
        return None
    try:
        _, found = identity_of(claim)                   # unreadable or gone: not ours to move
    except OSError:
        return None
    canonical = results_dir / f"{c.stem}.txt"
    if canonical.exists() and _same_file(claim, canonical):
        _drop_link(claim, log, c.stem, results_dir)     # a link-fallback put-back, finished
        return canonical
    for copy in undelivered_quarantine.find_quarantined(results_dir, c.stem):
        if _same_file(claim, copy):
            _drop_link(claim, log, c.stem, results_dir)
            return copy
    unverified = (found.ino, found.mtime_ns, found.digest) != (c.ino, c.mtime_ns, c.digest)
    if c.restore or unverified:
        try:
            if put_back(claim, canonical, log):
                if not quiet:
                    what = "a reply the owner never verified" if unverified and not c.restore \
                        else "a reply left in an interrupted put-back"
                    log(f"result {c.stem}: restored {what}")
                return canonical
        except FileNotFoundError:
            return None                                 # another observer got there first
        why = "a superseded reply left in an interrupted put-back was kept as"
    else:
        why = "recovered a result left in an interrupted disposal — quarantined to"
    target = _quarantine_target(results_dir, c.stem)
    try:
        os.rename(claim, target)
    except FileNotFoundError:
        return None
    if not quiet:
        where = target.name if (c.restore or unverified) else f"{undelivered_quarantine.DIRNAME}/"
        log(f"result {c.stem}: {why} {where}")
    return target


def _report_once(key: str, log: Log, line: str) -> None:
    if key not in _REPORTED:
        _REPORTED.add(key)
        log(line)


def recover_abandoned_claims(results_dir: Path, log: Log) -> None:
    """Every pass: no result body may stay stranded behind an owner that cannot
    finish. One claim's failure is reported once and never stops the others;
    a pass that cannot take the lock, or has no directory to lock, skips and
    never blocks the delivery that follows it."""
    results_dir = Path(results_dir)
    if not results_dir.is_dir():
        return
    for odd in find_malformed(results_dir):
        _report_once(f"malformed:{odd}", log, f"result file {odd.name}: a claim of an unknown "
                                              "shape — left alone")
    try:
        with locked(results_dir):
            for claim in find_claims(results_dir):
                try:
                    if not owner_holds(claim):
                        _recover_claim(results_dir, claim, log)
                except Exception as e:  # noqa: BLE001 - isolation is the point
                    _report_once(str(claim), log, f"result {parse_claim(claim).stem}: could not "
                                                  f"recover {claim.name} ({e}) — left for the operator")
    except OSError as e:                                # DisposalBusy, ENOLCK, EACCES, a vanished dir
        _report_once(f"lock:{results_dir}", log, f"result disposal: {e}; recovery skipped this pass")


def disposed_copy_exists(results_dir: Path, tid: str, generation: ResultIdentity,
                         log: Log) -> bool:
    """True when the very file this pass read (same inode, write time and
    bytes) already sits in quarantine or in a claim a live owner is still
    moving; a claim nobody holds is recovered instead of trusted. Equal bytes
    under another inode, or under a reused inode with a new write time, are a
    distinct publication and never count. Scanned twice because a claim can
    become a quarantined copy between the two listings."""
    results_dir = Path(results_dir)
    stem = f"task-{tid}" if not str(tid).startswith("task-") else str(tid)
    with locked(results_dir):
        for _ in range(2):
            for p in undelivered_quarantine.find_quarantined(results_dir, tid):
                try:
                    if _is_generation(p, generation):
                        return True
                except OSError:
                    continue                            # an unreadable copy proves nothing
            for claim in find_claims(results_dir, stem):
                try:
                    if not _is_generation(claim, generation):
                        continue
                except OSError:
                    continue
                if owner_holds(claim):
                    return True
                if _recover_claim(results_dir, claim, log) is not None:
                    return True
    return False
