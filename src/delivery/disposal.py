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
(`results/.disposal.lock`). What the lock cannot cover is the producer, which
writes results without it, so the steps that touch the canonical name stay
conditional (`os.link` refuses a taken name; a claim is unlinked only while a
second link of it exists).

The move is two renames through a private claim,
`results/.<stem>.disposing-<pid>-<birth-usec>-<acquired-s>-<gen16>-<nonce>[.restore]`.
The name carries the owner's birth token (a recycled pid is not an owner), the
acquisition time (a rename keeps the result's own mtime, which says when the
reply was written, not when it was claimed) and the first 16 hex of the digest
the owner meant to dispose of, so a recovery pass knows whether a stranded body
is that generation (quarantine it) or a reply the owner never verified (put it
back live). `.restore` records that the owner was putting one back. A death
between the renames leaves a body at the claim path; every pass recovers the
claims whose owner cannot finish, so no reply is stranded where the operator
cannot see it, and one damaged claim never blocks the others.

Dependency-light: the caller supplies its results directory and a log callback.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
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

__all__ = ["CLAIM_MAX_S", "LOCK_WAIT_S", "LOCK_NAME", "ACTIVE_CLAIMS", "Claim",
           "GenerationReplaced", "DisposalBusy", "locked", "self_token", "parse_claim",
           "find_claims", "find_malformed", "owner_holds", "quarantine_generation",
           "put_back", "recover_claim", "recover_abandoned_claims", "disposed_copy_exists"]

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

_NAME = ".{stem}.disposing-{pid}-{start}-{acquired}-{gen}-{nonce}"
_RESTORE = ".restore"
_RE = re.compile(r"^\.(?P<stem>task-.+)\.disposing-(?P<pid>\d+)-(?P<start>\d+)-"
                 r"(?P<acquired>\d+)-(?P<gen>[0-9a-f]{16})-(?P<nonce>[0-9a-f]+)"
                 r"(?P<intent>\.restore)?$")

Log = Callable[[str], None]


class Claim(NamedTuple):
    path: Path
    stem: str
    pid: int
    start: int
    acquired: int
    gen: str
    restore: bool


class GenerationReplaced(FileNotFoundError):
    """The file this observer read is no longer at its name: a newer reply is
    (or was put back) there, and it stays live."""


class DisposalBusy(OSError):
    """The results directory's disposal lock stayed held for the whole wait."""


@contextlib.contextmanager
def locked(results_dir: Path) -> Iterator[None]:
    """One exclusive lock per results directory, re-entrant within a thread.
    Two threads of one process lock two open file descriptions, so the drain
    and the sweep serialize exactly like two processes do."""
    depth = getattr(_HELD, "depth", 0)
    if depth:
        _HELD.depth = depth + 1
        try:
            yield
        finally:
            _HELD.depth -= 1
        return
    fd = os.open(Path(results_dir) / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DisposalBusy(f"{LOCK_NAME} held for more than {LOCK_WAIT_S:.0f}s")
                time.sleep(0.02)
        _HELD.depth = 1
        try:
            yield
        finally:
            _HELD.depth = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def self_token() -> "tuple[int, int]":
    ident = process_identity(os.getpid())
    return os.getpid(), int(ident.start_usec or 0)


def parse_claim(path: Path) -> Optional[Claim]:
    m = _RE.match(Path(path).name)
    if not m:
        return None
    return Claim(Path(path), m.group("stem"), int(m.group("pid")), int(m.group("start")),
                 int(m.group("acquired")), m.group("gen"), bool(m.group("intent")))


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
    """Remove a claim that should be a second name of a restored or
    quarantined file. If the canonical name was retaken meanwhile the claim
    is that reply's last link: it is kept where the operator looks instead."""
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


def _quarantine_generation(results_dir: Path, rfile: Path, generation: ResultIdentity,
                           log: Log) -> Path:
    pid, start = self_token()
    claim = rfile.with_name(_NAME.format(stem=rfile.stem, pid=pid, start=start,
                                         acquired=int(time.time()), gen=generation.digest[:16],
                                         nonce=uuid.uuid4().hex[:8]))
    ACTIVE_CLAIMS.add(str(claim))
    try:
        os.rename(rfile, claim)                       # FileNotFoundError: nothing there
        try:
            matches = _is_generation(claim, generation)
            if matches:
                target = _quarantine_target(results_dir, rfile.stem)
                os.rename(claim, target)
                return target
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
    """Return the claimed body to its canonical name; False when that name is
    taken again (os.link refuses atomically where rename would replace). The
    claim is dropped only while the canonical name still links the same
    inode; if a producer retook the name in between, the claim is the reply's
    last link and is kept in quarantine instead of unlinked."""
    try:
        os.link(claim, rfile)
    except FileExistsError:
        return False
    _drop_link(Path(claim), log or (lambda _m: None), Path(rfile).stem, Path(rfile).parent)
    return True


def recover_claim(results_dir: Path, claim: Path, log: Log, quiet: bool = False) -> Optional[Path]:
    """Finish what an abandoned claim's owner could not: a body the owner
    never verified (its digest is not the one named in the claim) or a
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
        _drop_link(claim, log, c.stem, results_dir)     # an interrupted put-back, finished
        return canonical
    for copy in undelivered_quarantine.find_quarantined(results_dir, c.stem):
        if _same_file(claim, copy):
            _drop_link(claim, log, c.stem, results_dir)
            return copy
    unverified = not found.digest.startswith(c.gen)
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
    a pass that cannot take the lock skips, named once."""
    results_dir = Path(results_dir)
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
    except DisposalBusy as e:
        _report_once(f"busy:{results_dir}", log, f"result disposal: {e}; recovery skipped this pass")


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
