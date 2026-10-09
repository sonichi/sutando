#!/usr/bin/env python3
"""Terminal disposal of a result file: claim the generation read, verify it,
quarantine it once; recover the claims a crashed owner left behind.

A terminal outbox decision moves its live `results/<task-id>.txt` aside exactly
once. Several observers (the outbound drain, the orphan sweep, a restarted
process) can reach the same decision, and a producer can publish a newer reply
at the same name at any moment, so disposal is bound to the exact file an
observer read (`ResultIdentity`: inode + bytes), never to the pathname.

The move is two renames through a private claim,
`results/.<stem>.disposing-<pid>-<birth-usec>-<acquired-s>-<nonce>[.restore]`.
The name carries the owner's birth token (a recycled pid is not an owner) and
the acquisition time (a rename keeps the result's own mtime, which says when
the reply was written, not when it was claimed). `.restore` records that the
owner found a newer reply under the claim and was putting it back. A death
between the renames leaves a body at the claim path; every pass recovers the
claims whose owner cannot finish, so no reply is stranded where the operator
cannot see it, and one damaged claim never blocks the others.

Dependency-light: the caller supplies its results directory and a log callback.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable, NamedTuple, Optional

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

__all__ = ["CLAIM_MAX_S", "ACTIVE_CLAIMS", "Claim", "GenerationReplaced",
           "self_token", "parse_claim", "find_claims", "owner_holds",
           "quarantine_generation", "put_back", "recover_claim",
           "recover_abandoned_claims", "disposed_copy_exists"]

# A disposal is a few renames; a claim this old under a live owner is stuck.
CLAIM_MAX_S = 600.0
# Claims THIS process is moving right now; one it made but no longer holds was abandoned.
ACTIVE_CLAIMS: "set[str]" = set()
# Claims whose recovery already failed in this process: reported once, not per pass.
_REPORTED: "set[str]" = set()

_NAME = ".{stem}.disposing-{pid}-{start}-{acquired}-{nonce}"
_RESTORE = ".restore"
_RE = re.compile(r"^\.(?P<stem>task-.+)\.disposing-(?P<pid>\d+)-(?P<start>\d+)-"
                 r"(?P<acquired>\d+)-(?P<nonce>[0-9a-f]+)(?P<intent>\.restore)?$")

Log = Callable[[str], None]


class Claim(NamedTuple):
    path: Path
    stem: str
    pid: int
    start: int
    acquired: int
    restore: bool


class GenerationReplaced(FileNotFoundError):
    """The file this observer read is no longer at its name: a newer reply is
    (or was put back) there, and it stays live."""


def self_token() -> "tuple[int, int]":
    ident = process_identity(os.getpid())
    return os.getpid(), int(ident.start_usec or 0)


def parse_claim(path: Path) -> Optional[Claim]:
    m = _RE.match(Path(path).name)
    if not m:
        return None
    return Claim(Path(path), m.group("stem"), int(m.group("pid")), int(m.group("start")),
                 int(m.group("acquired")), bool(m.group("intent")))


def find_claims(results_dir: Path, stem: Optional[str] = None) -> "list[Path]":
    pattern = f".{stem}.disposing-*" if stem else ".task-*.disposing-*"
    try:
        return sorted(p for p in Path(results_dir).glob(pattern) if parse_claim(p))
    except OSError:
        return []


def owner_holds(claim: Path, now: Optional[float] = None) -> bool:
    """True while the claim's owner may still finish the move. Liveness first:
    a dead or recycled pid never holds, whatever the age; a live owner holds
    until the acquisition bound. Our own pid holds only what it is moving."""
    c = parse_claim(claim)
    if c is None:
        return True                                     # unknown shape: never touch
    if c.pid == os.getpid():
        return str(claim) in ACTIVE_CLAIMS
    owner = process_identity(c.pid)
    if owner.state is OwnerState.DEAD:
        return False
    if owner.state is OwnerState.ALIVE and c.start and owner.start_usec and c.start != owner.start_usec:
        return False                                    # the pid was reused
    return (now if now is not None else time.time()) - c.acquired <= CLAIM_MAX_S


def _same_file(a: Path, b: Path) -> bool:
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _is_generation(path: Path, generation: ResultIdentity) -> bool:
    """`path` is the very publication `generation` describes: same inode and bytes."""
    try:
        st = os.stat(path)
        if (st.st_dev, st.st_ino) != (generation.dev, generation.ino):
            return False
        return identity_of(path)[1] == generation
    except OSError:
        return False


def _quarantine_target(results_dir: Path, stem: str) -> Path:
    d = undelivered_quarantine.quarantine_dir(results_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d / undelivered_quarantine.quarantine_name(stem)


def quarantine_generation(results_dir: Path, rfile: Path, generation: ResultIdentity,
                          log: Log) -> Path:
    """Quarantine `rfile` only if it still IS `generation`. The rename to a
    private claim is the atomic step; a different file found there is a newer
    reply and goes back under a recorded `.restore` intent. A failure after
    the claim rename puts the body back or quarantines it: the claim path is
    never where a result ends up."""
    rfile = Path(rfile)
    pid, start = self_token()
    claim = rfile.with_name(_NAME.format(stem=rfile.stem, pid=pid, start=start,
                                         acquired=int(time.time()), nonce=uuid.uuid4().hex[:8]))
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
            if put_back(claim, rfile):
                raise
            return recover_claim(results_dir, claim, log, quiet=True) or rfile
        restoring = claim.with_name(claim.name + _RESTORE)
        os.rename(claim, restoring)                   # the intent survives a crash
        ACTIVE_CLAIMS.add(str(restoring))
        try:
            if put_back(restoring, rfile):
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


def put_back(claim: Path, rfile: Path) -> bool:
    """Return the claimed body to its canonical name; False when that name is
    taken again (os.link refuses atomically where rename would replace)."""
    try:
        os.link(claim, rfile)
    except FileExistsError:
        return False
    os.unlink(claim)
    return True


def recover_claim(results_dir: Path, claim: Path, log: Log, quiet: bool = False) -> Optional[Path]:
    """Finish what an abandoned claim's owner could not: a `.restore` claim goes
    back to its canonical name, any other goes where the operator looks. A
    claim that is merely a second name of a file already restored or
    quarantined is dropped. Returns the path the body ended at, None when
    nothing was left to recover."""
    c = parse_claim(claim)
    if c is None:
        return None
    try:
        identity_of(claim)                              # unreadable or gone: not ours to move
    except OSError:
        return None
    results_dir = Path(results_dir)
    canonical = results_dir / f"{c.stem}.txt"
    if canonical.exists() and _same_file(claim, canonical):
        claim.unlink()                                  # an interrupted put-back, finished
        return canonical
    for copy in undelivered_quarantine.find_quarantined(results_dir, c.stem):
        if _same_file(claim, copy):
            claim.unlink()
            return copy
    if c.restore:
        try:
            if put_back(claim, canonical):
                if not quiet:
                    log(f"result {c.stem}: restored a reply left in an interrupted put-back")
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
        where = target.name if c.restore else f"{undelivered_quarantine.DIRNAME}/"
        log(f"result {c.stem}: {why} {where}")
    return target


def recover_abandoned_claims(results_dir: Path, log: Log) -> None:
    """Every pass: no result body may stay stranded behind an owner that cannot
    finish. One claim's failure is reported once and never stops the others."""
    for claim in find_claims(results_dir):
        try:
            if not owner_holds(claim):
                recover_claim(results_dir, claim, log)
        except Exception as e:  # noqa: BLE001 - isolation is the point
            key = str(claim)
            if key not in _REPORTED:
                _REPORTED.add(key)
                log(f"result {parse_claim(claim).stem}: could not recover {claim.name} "
                    f"({e}) — left for the operator")


def disposed_copy_exists(results_dir: Path, tid: str, generation: ResultIdentity,
                         log: Log) -> bool:
    """True when the very file this pass read (same inode and bytes) already
    sits in quarantine or in a claim a live owner is still moving; a claim
    nobody holds is recovered instead of trusted. Equal bytes under another
    inode are a distinct publication and never count. Scanned twice because a
    claim can become a quarantined copy between the two listings."""
    results_dir = Path(results_dir)
    stem = f"task-{tid}" if not str(tid).startswith("task-") else str(tid)
    for _ in range(2):
        for p in undelivered_quarantine.find_quarantined(results_dir, tid):
            if _is_generation(p, generation):
                return True
        for claim in find_claims(results_dir, stem):
            if not _is_generation(claim, generation):
                continue
            if owner_holds(claim):
                return True
            if recover_claim(results_dir, claim, log) is not None:
                return True
    return False
