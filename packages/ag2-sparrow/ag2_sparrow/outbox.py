"""Sparrow Outbox: durable delivery claims for an already-created outbound item.

The production boundary starts at an OutboundItem. Deciding which agents receive
a room event, and which items therefore exist, is upstream server-side semantics
and deliberately invisible here.

Scope is local drainer exclusion, keyed (outbox_instance, item_id): among the
drainers sharing one outbox, exactly one sends any given item. It says nothing
about who owns the originating task.

Two rules, both about not-knowing:
  * process_identity returns ALIVE / DEAD / UNKNOWN, never a bool. EPERM means
    alive-but-opaque; collapsing it to DEAD steals a live owner's claim.
  * An unknown delivery outcome is not a failed delivery. It parks. A bare
    ok-with-no-id may already have been delivered, so retrying duplicates
    rather than repairs.

Contracts: tests/sparrow-outbox-claim-protocol.test.py, tests/outbox-race-check.py
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

CLAIMS_DIR = ".claims"
ITEMS_DIR = ".items"


# -- three-state process identity ---------------------------------------------

class OwnerState(str, Enum):
    ALIVE = "ALIVE"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    state: OwnerState
    start_usec: Optional[int] = None


_PROC_PIDTBSDINFO = 3
_PROC_BSDINFO_SIZE = 136          # measured; verified against ctypes.sizeof below


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _linux_process_identity(pid: int) -> Optional[ProcessIdentity]:
    """Linux identity from /proc. Returns None when this is not a /proc system.

    Field 22 of /proc/<pid>/stat is the process start time in clock ticks since
    boot; combined with /proc/stat's btime it yields an absolute start instant.
    Resolution is one clock tick (typically 10ms) rather than the microsecond
    Darwin gives, which is coarser but still distinguishes a recycled pid from
    the original in every case a delivery claim cares about.

    The comm field can contain spaces and parentheses, so the fields after it
    are located from the LAST ')' rather than by splitting the whole line.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
    except FileNotFoundError:
        if not os.path.isdir("/proc/self"):
            return None                      # not a /proc system at all
        return ProcessIdentity(pid, OwnerState.DEAD)
    except PermissionError:
        return ProcessIdentity(pid, OwnerState.UNKNOWN)
    except OSError:
        return ProcessIdentity(pid, OwnerState.UNKNOWN)
    try:
        after_comm = raw[raw.rindex(")") + 1:].split()
        start_ticks = int(after_comm[19])            # field 22, 1-based
        hz = os.sysconf("SC_CLK_TCK") or 100
        btime = 0
        with open("/proc/stat", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    btime = int(line.split()[1])
                    break
        usec = (btime + start_ticks / hz) * 1_000_000 if btime else None
        return ProcessIdentity(pid, OwnerState.ALIVE,
                               int(usec) if usec is not None else None)
    except (ValueError, IndexError, OSError):
        # It exists — we just could not parse its birth time. Alive without a
        # token is still ALIVE; claiming UNKNOWN here would block reclamation.
        return ProcessIdentity(pid, OwnerState.ALIVE)


def process_identity(pid: int) -> ProcessIdentity:
    """ALIVE / DEAD / UNKNOWN for a pid, with a microsecond birth token when visible.

    Never returns a bool. EPERM means alive-but-opaque, which is UNKNOWN — the
    one case a two-state probe gets catastrophically wrong, because "unknown"
    then reads as "dead" and the claim gets stolen from a running worker.
    """
    linux = _linux_process_identity(pid)
    if linux is not None:
        return linux
    # Darwin fallback; the /proc branch above wins wherever /proc exists.
    try:  # pragma: no cover - macOS-only, unmeasurable on the Linux CI runner
        libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib",
                              use_errno=True)
    except OSError:
        return ProcessIdentity(pid, OwnerState.UNKNOWN)

    info = _ProcBsdInfo()
    ctypes.set_errno(0)
    size = ctypes.sizeof(info)
    ret = libproc.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(_PROC_PIDTBSDINFO),
                               ctypes.c_uint64(0), ctypes.byref(info), ctypes.c_int(size))
    if ret == size:
        return ProcessIdentity(pid, OwnerState.ALIVE,
                               int(info.pbi_start_tvsec) * 1_000_000 + int(info.pbi_start_tvusec))
    err = ctypes.get_errno()
    if err == errno.ESRCH:
        return ProcessIdentity(pid, OwnerState.DEAD)
    # EPERM, and anything else we cannot interpret, are both UNKNOWN. Defaulting
    # an unrecognised errno to DEAD would be the same theft by another route.
    return ProcessIdentity(pid, OwnerState.UNKNOWN)


# -- delivery outcomes --------------------------------------------------------

class DeliveryOutcome(str, Enum):
    CONFIRMED = "CONFIRMED"
    NOT_DELIVERED = "NOT_DELIVERED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class RetrySafety(str, Enum):
    SAFE = "SAFE"          # idempotent destination: a repeat cannot duplicate
    UNSAFE = "UNSAFE"      # a repeat may deliver twice


MAX_ATTEMPTS = 5


def resolve_outcome(outcome: DeliveryOutcome, safety: RetrySafety,
                    attempts: int = 0) -> str:
    """-> "done" | "retry" | "park".

    OUTCOME_UNKNOWN + UNSAFE parks: an accepted send that returns no id may
    already have landed, so every retry delivers again rather than repairing.
    """
    if outcome == DeliveryOutcome.CONFIRMED:
        return "done"
    if outcome == DeliveryOutcome.NOT_DELIVERED:
        return "retry" if attempts < MAX_ATTEMPTS else "park"
    if safety == RetrySafety.SAFE:
        return "retry" if attempts < MAX_ATTEMPTS else "park"
    return "park"


# -- claim records ------------------------------------------------------------

@dataclass(frozen=True)
class ClaimRecord:
    item_id: str
    drainer_id: str
    pid: int
    start_usec: Optional[int]
    claimed_at: float
    state: str = "HELD"          # HELD | UNKNOWN (torn)


def _claims_dir(root: Path) -> Path:
    return Path(root) / CLAIMS_DIR


def _safe_key(item_id: str) -> str:
    """Filesystem-safe AND injective: distinct ids never share a path.

    The readable part is lossy ("a/b" and "a_b" both sanitize to "a_b"), so the
    digest of the raw id decides identity; without it two unrelated items share
    one claim and one is denied delivery.
    """
    readable = "".join(c if (c.isalnum() or c in "-._") else "_" for c in item_id)[:80]
    return f"{readable}.{hashlib.sha256(item_id.encode('utf-8')).hexdigest()[:16]}"


def _claim_path(root: Path, item_id: str) -> Path:
    return _claims_dir(root) / f"{_safe_key(item_id)}.claim"


def acquire_delivery_claim(root: Path, item_id: str, drainer_id: str) -> bool:
    """Exactly one drainer wins, via O_CREAT|O_EXCL on one canonical key.

    Local drainer exclusion ONLY. Lifting this to the room layer would make one
    agent's delivery revoke another's eligibility, which the room invariant
    forbids.
    """
    root = Path(root)
    d = _claims_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    p = _claim_path(root, item_id)
    ident = process_identity(os.getpid())
    payload = json.dumps({
        "item_id": item_id, "drainer_id": drainer_id, "pid": os.getpid(),
        "start_usec": ident.start_usec, "claimed_at": time.time(),
    }, sort_keys=True)
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        # Leave the (possibly torn) file in place: read_delivery_claim reports it
        # UNKNOWN. Removing it here would make a crash look like "never claimed".
        raise
    return True


def read_delivery_claim(root: Path, item_id: str) -> Optional[ClaimRecord]:
    """The claim, or None if absent. A TORN claim reads UNKNOWN, never absent.

    Absent means free. Truncated means someone crashed mid-write and we do not
    know what they had done — collapsing that to "free" is how a half-finished
    delivery gets repeated.
    """
    return _read_claim_at(_claim_path(Path(root), item_id), item_id)


def _read_claim_at(p: Path, item_id: str) -> Optional[ClaimRecord]:
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    if not raw.strip():
        return ClaimRecord(item_id, "", -1, None, 0.0, state="UNKNOWN")
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return ClaimRecord(item_id, "", -1, None, 0.0, state="UNKNOWN")
    if not isinstance(d, dict):
        return ClaimRecord(item_id, "", -1, None, 0.0, state="UNKNOWN")
    # Valid JSON carrying wrong types is still a claim we cannot read. Raising
    # here would abort a drainer mid-scan; UNKNOWN keeps the item un-stealable.
    try:
        pid = int(d.get("pid", -1))
        claimed_at = float(d.get("claimed_at", 0.0))
        raw_start = d.get("start_usec")
        start_usec = None if raw_start is None else int(raw_start)
    except (TypeError, ValueError):
        return ClaimRecord(item_id, "", -1, None, 0.0, state="UNKNOWN")
    return ClaimRecord(item_id=str(d.get("item_id", item_id)),
                       drainer_id=str(d.get("drainer_id", "")),
                       pid=pid,
                       start_usec=start_usec,
                       claimed_at=claimed_at)


def may_reclaim_delivery(root: Path, item_id: str, ttl_seconds: float) -> bool:
    """May another drainer take this claim?

    TTL alone is never sufficient. A slow delivery is not a dead worker, so the
    owner's liveness is checked first and an ALIVE or UNKNOWN owner is never
    displaced. Only a DEAD owner past the TTL releases the item.
    """
    rec = read_delivery_claim(Path(root), item_id)
    if rec is None:
        return True                       # nothing holds it
    return _record_is_reclaimable(rec, ttl_seconds)


def _record_is_reclaimable(rec: ClaimRecord, ttl_seconds: float) -> bool:
    if rec.state == "UNKNOWN":
        return False                      # torn: we do not know; do not steal
    owner = process_identity(rec.pid)
    if owner.state is OwnerState.UNKNOWN:
        return False
    if owner.state is OwnerState.ALIVE and not _pid_was_reused(rec, owner):
        return False
    return (time.time() - rec.claimed_at) >= ttl_seconds


def _pid_was_reused(rec: ClaimRecord, owner: ProcessIdentity) -> bool:
    """Is the pid ALIVE but a DIFFERENT process than the one that claimed?

    Without this the birth token is dead weight: a recycled pid reads ALIVE, the
    liveness check returns early, and the TTL never gets a say — the item stalls
    forever rather than being redelivered.
    """
    if rec.start_usec is None or owner.start_usec is None:
        return False                      # cannot tell: treat as the same owner
    return rec.start_usec != owner.start_usec


def _same_claim(a: ClaimRecord, b: ClaimRecord) -> bool:
    return (a.drainer_id, a.pid, a.start_usec, a.claimed_at) == \
           (b.drainer_id, b.pid, b.start_usec, b.claimed_at)


def _claim_stamp(rec: ClaimRecord) -> str:
    """One name per claim INSTANCE. A unique name per caller would let every
    drainer win its own swap, which is not a compare-and-swap at all."""
    token = f"{rec.drainer_id}|{rec.pid}|{rec.start_usec}|{rec.claimed_at!r}"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def reclaim_delivery_claim(root: Path, item_id: str, ttl_seconds: float,
                           drainer_id: str) -> bool:
    """Atomically take over a reclaimable claim; True if THIS caller now holds it.

    `may_reclaim_delivery` + release + acquire is check-then-act: two drainers
    can both observe the same stale claim, and the second's release deletes the
    first's fresh claim, so both then hold the item. The rename below is the
    compare-and-swap — only one caller can move a given file.
    """
    root = Path(root)
    observed = read_delivery_claim(root, item_id)
    if observed is None:
        return acquire_delivery_claim(root, item_id, drainer_id)
    if not _record_is_reclaimable(observed, ttl_seconds):
        return False
    src = _claim_path(root, item_id)
    # The name is derived from the OBSERVED record, so every drainer judging the
    # same claim competes for one name and O_EXCL-on-link picks a single winner.
    tomb = src.with_name(f"{src.name}.reclaim-{_claim_stamp(observed)}")
    try:
        os.link(str(src), str(tomb))
    except OSError:
        return False                      # another drainer judged this same record
    taken = _read_claim_at(src, item_id)
    if taken is None or not _same_claim(taken, observed):
        return False                      # it moved on; our observation is stale
    try:
        os.unlink(str(src))
    except FileNotFoundError:
        return False
    return acquire_delivery_claim(root, item_id, drainer_id)


def release_delivery_claim(root: Path, item_id: str, drainer_id: Optional[str] = None,
                           *, force: bool = False) -> bool:
    """Release a claim; True if one was removed.

    Ownership-checked by default. An unconditional unlink is exactly what let a
    losing drainer delete the winner's claim, so the destructive form is named.
    """
    if drainer_id is None and not force:
        raise ValueError("release_delivery_claim needs a drainer_id, or force=True")
    root = Path(root)
    p = _claim_path(root, item_id)
    if not force:
        rec = _read_claim_at(p, item_id)
        # A torn claim has no readable owner, so nobody can prove they hold it.
        if rec is None or rec.state == "UNKNOWN" or rec.drainer_id != drainer_id:
            return False
    released = True
    try:
        p.unlink()
    except FileNotFoundError:
        released = False
    # The swap names are CAS tokens, not state. They are only meaningful while a
    # peer could still hold the observation they encode, which ends here.
    for stale in _claims_dir(root).glob(f"{p.name}.reclaim-*"):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    return released


# -- item lifecycle -----------------------------------------------------------

def _items_dir(root: Path) -> Path:
    return Path(root) / ITEMS_DIR


def _item_path(root: Path, item_id: str) -> Path:
    return _items_dir(root) / f"{_safe_key(item_id)}.json"


def _read_item(root: Path, item_id: str) -> dict:
    try:
        return json.loads(_item_path(root, item_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"item_id": item_id, "attempts": 0, "status": "QUEUED", "reason": None}


def _write_item(root: Path, item_id: str, data: dict) -> None:
    p = _item_path(Path(root), item_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def attempts_for(root: Path, item_id: str) -> int:
    return int(_read_item(Path(root), item_id).get("attempts", 0))


def note_attempt(root: Path, item_id: str) -> int:
    d = _read_item(Path(root), item_id)
    d["attempts"] = int(d.get("attempts", 0)) + 1
    _write_item(Path(root), item_id, d)
    return d["attempts"]


def park_item(root: Path, item_id: str, reason: str = "") -> None:
    d = _read_item(Path(root), item_id)
    d["status"] = "PARKED"
    d["reason"] = reason
    _write_item(Path(root), item_id, d)


def requeue_item(root: Path, item_id: str) -> None:
    """Hand-recovered item returns to the queue with a FULL attempt budget.

    Carrying the old count forward makes a re-queued item park again instantly,
    which is indistinguishable from a broken destination and hides the recovery.
    """
    d = _read_item(Path(root), item_id)
    d["status"] = "QUEUED"
    d["attempts"] = 0
    d["reason"] = None
    _write_item(Path(root), item_id, d)
    release_delivery_claim(Path(root), item_id, force=True)
