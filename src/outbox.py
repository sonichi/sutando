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


def _claim_path(root: Path, item_id: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-._") else "_" for c in item_id)
    return _claims_dir(root) / f"{safe}.claim"


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
    p = _claim_path(Path(root), item_id)
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
    return ClaimRecord(item_id=d.get("item_id", item_id),
                       drainer_id=d.get("drainer_id", ""),
                       pid=int(d.get("pid", -1)),
                       start_usec=d.get("start_usec"),
                       claimed_at=float(d.get("claimed_at", 0.0)))


def may_reclaim_delivery(root: Path, item_id: str, ttl_seconds: float) -> bool:
    """May another drainer take this claim?

    TTL alone is never sufficient. A slow delivery is not a dead worker, so the
    owner's liveness is checked first and an ALIVE or UNKNOWN owner is never
    displaced. Only a DEAD owner past the TTL releases the item.
    """
    rec = read_delivery_claim(Path(root), item_id)
    if rec is None:
        return True                       # nothing holds it
    if rec.state == "UNKNOWN":
        return False                      # torn: we do not know; do not steal
    owner = process_identity(rec.pid)
    if owner.state in (OwnerState.ALIVE, OwnerState.UNKNOWN):
        return False
    return (time.time() - rec.claimed_at) >= ttl_seconds


def release_delivery_claim(root: Path, item_id: str) -> None:
    try:
        _claim_path(Path(root), item_id).unlink()
    except FileNotFoundError:
        pass


# -- item lifecycle -----------------------------------------------------------

def _items_dir(root: Path) -> Path:
    return Path(root) / ITEMS_DIR


def _item_path(root: Path, item_id: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-._") else "_" for c in item_id)
    return _items_dir(root) / f"{safe}.json"


def _read_item(root: Path, item_id: str) -> dict:
    try:
        return json.loads(_item_path(root, item_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"item_id": item_id, "attempts": 0, "state": "QUEUED", "reason": None}


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
    d["state"] = "PARKED"
    d["reason"] = reason
    _write_item(Path(root), item_id, d)


def requeue_item(root: Path, item_id: str) -> None:
    """Hand-recovered item returns to the queue with a FULL attempt budget.

    Carrying the old count forward makes a re-queued item park again instantly,
    which is indistinguishable from a broken destination and hides the recovery.
    """
    d = _read_item(Path(root), item_id)
    d["state"] = "QUEUED"
    d["attempts"] = 0
    d["reason"] = None
    _write_item(Path(root), item_id, d)
    release_delivery_claim(Path(root), item_id)
