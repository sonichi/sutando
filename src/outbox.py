"""Sparrow Outbox: durable claims and terminal receipts for outbound items.

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

import contextlib
import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Union

CLAIMS_DIR = ".claims"
LOCKS_DIR = ".claim-locks"
ITEMS_DIR = ".items"
TERMINAL_RECEIPTS_DIR = ".terminal-receipts"
TERMINAL_RECEIPT_SCHEMA = 2
TERMINAL_RECEIPT_TTL_S = 30 * 86400.0
TERMINAL_RECEIPT_MAX_RECORDS = 100_000
TERMINAL_RECEIPT_MAX_BYTES = 16 * 1024
TERMINAL_RECEIPT_SHARDS = 256
TERMINAL_RECEIPT_SWEEP_BATCH = 512


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
    return _darwin_process_identity(pid)


def _darwin_process_identity(pid: int) -> ProcessIdentity:  # pragma: no cover - macOS-only; the Linux gate takes the /proc path
    """Darwin fallback; the /proc branch wins wherever /proc exists."""
    try:
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


_HELD = threading.local()

# Bounds the lock namespace. Changing it remaps item->stripe, so mixed-value
# processes would not mutually exclude: a migration (restart world), not tuning.
LOCK_STRIPES = 64


def _lock_stripe(item_id: str) -> int:
    return int(hashlib.sha256(item_id.encode("utf-8")).hexdigest(), 16) % LOCK_STRIPES


STRIPES_FENCE = "stripes-active.json"


def _fence_path(root: Path) -> Path:
    return Path(root) / LOCKS_DIR / STRIPES_FENCE


_STRIPE_MODE: dict[str, bool] = {}


def _root_key(root: Path) -> str:
    """Canonical identity for a root. Every cache/guard keyed by root uses
    this: raw path spellings (dot-dot, symlinks) of one directory must never
    occupy separate entries, or aliases straddle lock namespaces."""
    return os.path.realpath(str(root))


def _stripe_mode(root: Path) -> bool:
    """True iff this root has been migrated to striped locking.

    The fence is the enforceable exclusivity proof: pre-striping processes
    lock per-item files, striped ones lock stripe files, and the two cannot
    exclude each other — so stripe mode is entered only via a fence written
    under whole-engine quiescence (activate_lock_striping). No fence = the
    pre-striping namespace, byte-compatible with older writers.

    Memoized per root for the process lifetime: activation happens only
    across a restart, so one process uses exactly one namespace — caching
    enforces that (no mid-flight namespace flip) and keeps lock acquisition
    free of per-call fence I/O. Only successful reads are cached; a corrupt
    fence keeps raising until it is fixed.
    """
    cached = _STRIPE_MODE.get(_root_key(root))
    if cached is not None:
        return cached
    fp = _fence_path(root)
    try:
        data = json.loads(fp.read_text())
    except FileNotFoundError:
        _STRIPE_MODE[_root_key(root)] = False
        return False
    except (OSError, ValueError) as e:
        raise RuntimeError(f"unreadable stripes fence {fp}: {e}") from e
    if data.get("stripes") != LOCK_STRIPES:
        # Mixed stripe counts are the same defect class as mixed namespaces.
        raise RuntimeError(
            f"stripes fence {fp} declares {data.get('stripes')!r}, this build "
            f"uses {LOCK_STRIPES}: migration required, refusing to guess")
    _STRIPE_MODE[_root_key(root)] = True
    return True


def activate_lock_striping(root: Path) -> bool:
    """Migrate this root to striped locking. True if this call activated it.

    CONTRACT: call ONLY while no other consumer of `root` runs (the deploy
    restart window) — the fence asserts that pre-striping lock holders are
    gone for good, which no runtime probe can prove. Idempotent; a fence
    declaring a different stripe count raises instead of being overwritten.
    """
    if _stripe_mode(root):                     # raises on count mismatch
        return False
    d = Path(root) / LOCKS_DIR
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{STRIPES_FENCE}.{os.getpid()}"
    tmp.write_text(json.dumps({"stripes": LOCK_STRIPES}))
    os.replace(str(tmp), str(_fence_path(root)))
    _STRIPE_MODE[_root_key(root)] = True
    return True


def _sweep_legacy_locks(d: Path) -> None:
    """One-shot upgrade sweep: pre-striping code left one `<key>.lock` per item.
    Runs only in stripe mode, whose fence contract guarantees no pre-striping
    process can still hold (or ever re-open) these files."""
    try:
        for f in d.iterdir():
            if f.name.endswith(".lock") and not f.name.startswith("stripe-"):
                f.unlink(missing_ok=True)
    except OSError:
        pass


@contextlib.contextmanager
def _item_lock(root: Path, item_id: str):
    """Serialize every mutation of one item's claim on a single primitive.

    A compare-then-act on a PATH cannot be made safe by narrowing it: the name
    can be rebound between the check and the act. flock closes the window
    outright, and the kernel drops it when the holder dies, so a crash cannot
    leave the item locked.

    Two namespaces, fence-selected: before activate_lock_striping runs, the
    pre-striping per-item files are used (cross-version safe during rolling
    upgrades); after, item -> stripe file bounds the namespace at LOCK_STRIPES
    inodes. Two items sharing a stripe serialize against each other — a
    contention cost, never a correctness one, because the stripe lock strictly
    contains the item lock.
    """
    striped = _stripe_mode(root)
    if striped:
        stripe = _lock_stripe(item_id)
        key = (_root_key(root), "stripe", stripe)
        lock_name = f"stripe-{stripe:02d}.lock"
    else:
        # Pre-migration: same per-item inode as pre-striping builds, so a
        # rolling-upgrade mix still mutually excludes. Unbounded until fenced.
        key = (_root_key(root), "item", item_id)
        lock_name = f"{_safe_key(item_id)}.lock"
    held = getattr(_HELD, "keys", None)
    if held is None:
        held = _HELD.keys = set()
    if key in held:
        # Re-entry (same item, or a stripe-mate) would have to bypass the lock
        # to avoid self-deadlock, and a bypass is the hole this primitive closes.
        raise RuntimeError(f"re-entrant claim operation on {item_id!r} ({key[1]})")
    # Locks live outside the claims directory: a lock file sharing that
    # namespace is picked up by anything globbing claim names.
    d = Path(root) / LOCKS_DIR
    d.mkdir(parents=True, exist_ok=True)
    if striped:
        if not getattr(_HELD, "swept_roots", None):
            _HELD.swept_roots = set()
        if _root_key(root) not in _HELD.swept_roots:
            _HELD.swept_roots.add(_root_key(root))
            _sweep_legacy_locks(d)
    fd = os.open(str(d / lock_name), os.O_CREAT | os.O_RDWR, 0o644)
    held.add(key)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        held.discard(key)
        os.close(fd)                      # closing releases the flock


def acquire_delivery_claim(root: Path, item_id: str, drainer_id: str) -> bool:
    """Take the delivery claim for an item; True only for the caller that won it."""
    with _item_lock(root, item_id):
        return _acquire_locked(root, item_id, drainer_id)


def _acquire_locked(root: Path, item_id: str, drainer_id: str) -> bool:
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
    """Take over a reclaimable claim; True if THIS caller now holds it."""
    with _item_lock(root, item_id):
        return _reclaim_locked(root, item_id, ttl_seconds, drainer_id)


def _reclaim_locked(root: Path, item_id: str, ttl_seconds: float,
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
        return _acquire_locked(root, item_id, drainer_id)
    if observed.state == "UNKNOWN":
        # A torn claim names no owner, so no age of it condemns a live writer —
        # but past the grace period its writer is gone and it would wedge forever.
        if not _sweep_if_abandoned(_claim_path(root, item_id)):
            return False
        return _acquire_locked(root, item_id, drainer_id)
    if not _record_is_reclaimable(observed, ttl_seconds):
        return False
    src = _claim_path(root, item_id)
    # The name is derived from the OBSERVED record, so every drainer judging the
    # same claim competes for one name and O_EXCL-on-link picks a single winner.
    tomb = src.with_name(f"{src.name}.reclaim-{_claim_stamp(observed)}")
    try:
        os.link(str(src), str(tomb))
    except OSError:
        # An honest swap finishes in microseconds, so a token older than the grace
        # period is a crashed reclaim; leaving it would wedge the item forever.
        if not _sweep_if_abandoned(tomb):
            return False                  # another drainer judged this same record
        try:
            os.link(str(src), str(tomb))
        except OSError:
            return False
    taken = _read_claim_at(src, item_id)
    if taken is None or not _same_claim(taken, observed):
        return False                      # it moved on; our observation is stale
    try:
        os.unlink(str(src))
    except FileNotFoundError:
        return False
    return _acquire_locked(root, item_id, drainer_id)


def release_delivery_claim(root: Path, item_id: str, drainer_id: Optional[str] = None,
                           *, force: bool = False) -> bool:
    """Release a claim; True if one was removed.

    Two modes with DIFFERENT concurrency contracts — oracles and tests must
    model them separately, never as one "release" op:

    - Ownership-safe (default, `drainer_id` given): removes only an instance
      this drainer verified at the same serialization point as the removal
      (both under `_item_lock`). May never destroy a peer's live claim.
    - `force=True` is ADMINISTRATIVE DESTRUCTION, not a release: it removes
      whatever claim instance occupies the slot at its own unlink instant,
      a live peer's included — that is its entitlement, not a defect — and
      never one created after it completes. It carries no ownership claim,
      so "force succeeded" tells an oracle the slot was cleared, nothing
      about who owned it.

    A verify-then-unlink whose verification happened at an earlier
    serialization point than its unlink belongs to NEITHER mode; that gap is
    the reclaim-after-preemption defect class the per-item lock closes.
    """
    with _item_lock(root, item_id):
        return _release_locked(root, item_id, drainer_id, force=force)


def _release_locked(root: Path, item_id: str, drainer_id: Optional[str] = None,
                    *, force: bool = False) -> bool:
    """Release a claim; True if one was removed.

    Ownership-checked by default. An unconditional unlink is exactly what let a
    losing drainer delete the winner's claim, so the destructive form is named.
    """
    if drainer_id is None and not force:
        raise ValueError("release_delivery_claim needs a drainer_id, or force=True")
    root = Path(root)
    p = _claim_path(root, item_id)
    released = _force_release(p) if force else _release_own_instance(p, item_id, drainer_id)
    if released:
        # Swap names are CAS tokens, not state: they mean something only while a
        # peer could still hold the observation they encode, which ends here.
        for stale in _claims_dir(root).glob(f"{p.name}.reclaim-*"):
            _discard(stale)
    return released


def _discard(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


# A swap token is held across two syscalls; anything older outlived its process.
SWAP_GRACE_S = 30.0


def _sweep_if_abandoned(tomb: Path, grace_s: float = SWAP_GRACE_S) -> bool:
    """Remove a swap token whose owner died mid-swap; True if one was removed."""
    try:
        age = time.time() - os.stat(str(tomb)).st_mtime
    except OSError:
        return True                       # already gone: the retry is free
    if age < grace_s:
        return False                      # a live peer is mid-swap right now
    try:
        os.unlink(str(tomb))
    except FileNotFoundError:
        pass
    return True


def _force_release(p: Path) -> bool:
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


def _release_own_instance(p: Path, item_id: str, drainer_id: str) -> bool:
    """Release only the claim INSTANCE this caller inspected.

    Link-then-verify, never rename: a rename moves whatever occupies the slot, so
    a stale observation evicts a successor and no later step can undo that.
    """
    observed = _read_claim_at(p, item_id)
    # A torn claim has no readable owner, so nobody can prove they hold it.
    if observed is None or observed.state == "UNKNOWN" or observed.drainer_id != drainer_id:
        return False
    tomb = p.with_name(f"{p.name}.release-{_claim_stamp(observed)}")
    try:
        os.link(str(p), str(tomb))
    except OSError:
        return False                      # a peer holds this same observation
    try:
        taken = _read_claim_at(tomb, item_id)
        if taken is None or not _same_claim(taken, observed):
            return False                  # never ours; the slot is left untouched

        # Same inode proves the slot did not turn over, so the unlink below
        # cannot remove a successor's claim.
        if not _same_inode(p, tomb):
            return False
        try:
            os.unlink(str(p))
        except FileNotFoundError:
            return False
        return True
    finally:
        _discard(tomb)


def _same_inode(a: Path, b: Path) -> bool:
    try:
        sa, sb = os.stat(str(a)), os.stat(str(b))
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


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


def record_delivered(root: Path, item_id: str, *, provider: Optional[str] = None,
                     destination: Optional[str] = None) -> None:
    """Mark an item delivered and persist WHERE it went.

    The log line naming provider/destination rotates; a receipt that omits them
    cannot answer "delivered to where" after that. Absent values are not stored,
    so items written before this existed read back as None rather than as a
    destination nobody observed.
    """
    d = _read_item(Path(root), item_id)
    d["status"] = "DELIVERED"
    if provider:
        d["provider"] = provider
    if destination:
        d["destination"] = destination
    _write_item(Path(root), item_id, d)


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

    Uses force-release (administrative destruction — see
    `release_delivery_claim`): a concurrent live delivery of this item may be
    interrupted and the item re-delivered after re-queue. At-least-once on this
    administrative path is accepted by design; operators invoke it exactly
    because normal ownership state can no longer be trusted.
    """
    d = _read_item(Path(root), item_id)
    d["status"] = "QUEUED"
    d["attempts"] = 0
    d["reason"] = None
    _write_item(Path(root), item_id, d)
    release_delivery_claim(Path(root), item_id, force=True)


# -- terminal receipts --------------------------------------------------------

class TerminalDisposition(str, Enum):
    DELIVERED = "delivered"
    NO_SEND = "no_send"
    DEDUPED = "deduped"
    REDIRECTED = "redirected"


class TerminalReceiptState(str, Enum):
    ABSENT = "ABSENT"
    TERMINAL = "TERMINAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TerminalReceipt:
    state: TerminalReceiptState
    item_id: str
    generation: int
    disposition: Optional[TerminalDisposition] = None
    recorded_at: Optional[float] = None
    content_digest: Optional[str] = None

    def __post_init__(self) -> None:
        terminal = self.state is TerminalReceiptState.TERMINAL
        if ((self.disposition is not None) is not terminal
                or (self.recorded_at is not None) is not terminal):
            raise ValueError("a terminal state has a disposition and timestamp")
        _validate_content_digest(self.content_digest)


@dataclass(frozen=True)
class TerminalReceiptCleanup:
    expired: int = 0
    overflow: int = 0
    stale_temps: int = 0
    unknown: int = 0
    kept: int = 0
    incomplete: bool = False


def _terminal_identity(item_id: str, generation: int) -> bytes:
    if not isinstance(item_id, str) or not item_id:
        raise ValueError("item_id must be a non-empty string")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("generation must be a non-negative integer")
    return json.dumps([item_id, generation], ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _terminal_digest(item_id: str, generation: int) -> str:
    return hashlib.sha256(_terminal_identity(item_id, generation)).hexdigest()


def _terminal_receipts_dir(root: Path) -> Path:
    return Path(root) / TERMINAL_RECEIPTS_DIR


def _terminal_receipt_shard(root: Path, digest: str) -> Path:
    return _terminal_receipts_dir(root) / digest[:2]


def _terminal_receipt_path(root: Path, item_id: str, generation: int) -> Path:
    digest = _terminal_digest(item_id, generation)
    return _terminal_receipt_shard(root, digest) / f"{digest}.json"


def _validate_content_digest(content_digest: Optional[str]) -> None:
    if content_digest is None:
        return
    if (not isinstance(content_digest, str) or len(content_digest) != 64
            or any(c not in "0123456789abcdef" for c in content_digest)):
        raise ValueError("content_digest must be a lowercase SHA-256 digest")


def terminal_content_digest(content: Union[str, bytes]) -> str:
    if isinstance(content, str):
        encoded = content.encode("utf-8")
    elif isinstance(content, bytes):
        encoded = content
    else:
        raise ValueError("content must be text or bytes")
    return hashlib.sha256(encoded).hexdigest()


def terminal_receipt_content_state(
    receipt: TerminalReceipt,
    content_digest: str,
) -> TerminalReceiptState:
    """Match a candidate body without treating digest-less evidence as absent."""
    _validate_content_digest(content_digest)
    if receipt.state is not TerminalReceiptState.TERMINAL:
        return receipt.state
    if receipt.content_digest is None:
        return TerminalReceiptState.UNKNOWN
    if receipt.content_digest == content_digest:
        return TerminalReceiptState.TERMINAL
    return TerminalReceiptState.ABSENT


def _terminal_payload(item_id: str, generation: int,
                      disposition: TerminalDisposition, recorded_at: float,
                      content_digest: Optional[str] = None) -> dict:
    _validate_content_digest(content_digest)
    base = {
        "schema": TERMINAL_RECEIPT_SCHEMA,
        "item_id": item_id,
        "generation": generation,
        "disposition": disposition.value,
        "recorded_at": recorded_at,
        "content_digest": content_digest,
    }
    canonical = json.dumps(base, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    return {**base, "checksum": hashlib.sha256(canonical).hexdigest()}


def _terminal_bytes(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unknown_terminal(item_id: Optional[str], generation: Optional[int]) -> TerminalReceipt:
    return TerminalReceipt(TerminalReceiptState.UNKNOWN, item_id or "",
                           generation if generation is not None else 0)


def _read_terminal_path(path: Path, item_id: Optional[str] = None,
                        generation: Optional[int] = None) -> TerminalReceipt:
    try:
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(str(path), flags)
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return _unknown_terminal(item_id, generation)
            raw = fh.read(TERMINAL_RECEIPT_MAX_BYTES + 1)
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return TerminalReceipt(TerminalReceiptState.ABSENT, item_id or "",
                                   generation if generation is not None else 0)
        except OSError:
            return _unknown_terminal(item_id, generation)
        return _unknown_terminal(item_id, generation)
    except OSError:
        return _unknown_terminal(item_id, generation)
    if len(raw) > TERMINAL_RECEIPT_MAX_BYTES:
        return _unknown_terminal(item_id, generation)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _unknown_terminal(item_id, generation)
    fields = {"schema", "item_id", "generation", "disposition",
              "recorded_at", "content_digest", "checksum"}
    if not isinstance(data, dict) or set(data) != fields:
        return _unknown_terminal(item_id, generation)
    if data.get("schema") != TERMINAL_RECEIPT_SCHEMA:
        return _unknown_terminal(item_id, generation)
    stored_item = data.get("item_id")
    stored_generation = data.get("generation")
    recorded_at = data.get("recorded_at")
    content_digest = data.get("content_digest")
    if not isinstance(stored_item, str) or not stored_item:
        return _unknown_terminal(item_id, generation)
    if (isinstance(stored_generation, bool)
            or not isinstance(stored_generation, int) or stored_generation < 0):
        return _unknown_terminal(item_id, generation)
    if (isinstance(recorded_at, bool) or not isinstance(recorded_at, (int, float))
            or not math.isfinite(float(recorded_at))):
        return _unknown_terminal(item_id, generation)
    try:
        _validate_content_digest(content_digest)
    except ValueError:
        return _unknown_terminal(item_id, generation)
    if item_id is not None and stored_item != item_id:
        return _unknown_terminal(item_id, generation)
    if generation is not None and stored_generation != generation:
        return _unknown_terminal(item_id, generation)
    try:
        disposition = TerminalDisposition(data.get("disposition"))
    except (TypeError, ValueError):
        return _unknown_terminal(item_id, generation)
    expected_name = f"{_terminal_digest(stored_item, stored_generation)}.json"
    if path.name != expected_name or path.parent.name != expected_name[:2]:
        return _unknown_terminal(item_id, generation)
    base = {k: data[k] for k in fields if k != "checksum"}
    canonical = json.dumps(base, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    checksum = data.get("checksum")
    if not isinstance(checksum, str) or checksum != hashlib.sha256(canonical).hexdigest():
        return _unknown_terminal(item_id, generation)
    return TerminalReceipt(TerminalReceiptState.TERMINAL, stored_item,
                           stored_generation, disposition, float(recorded_at),
                           content_digest)


def _terminal_clock_and_ttl(now: Optional[float],
                            ttl_seconds: float) -> tuple[float, float]:
    clock = time.time() if now is None else float(now)
    ttl = float(ttl_seconds)
    if not math.isfinite(clock):
        raise ValueError("now must be finite")
    if not math.isfinite(ttl) or ttl < 0:
        raise ValueError("ttl_seconds must be finite and non-negative")
    return clock, ttl


def read_terminal_receipt(
    root: Path,
    item_id: str,
    generation: int = 0,
    now: Optional[float] = None,
    ttl_seconds: float = TERMINAL_RECEIPT_TTL_S,
) -> TerminalReceipt:
    """O(1) lookup. Corrupt or unreadable state is UNKNOWN, never ABSENT."""
    clock, ttl = _terminal_clock_and_ttl(now, ttl_seconds)
    receipt = _read_terminal_path(
        _terminal_receipt_path(Path(root), item_id, generation), item_id, generation)
    if (receipt.state is TerminalReceiptState.TERMINAL
            and clock - receipt.recorded_at >= ttl):
        return TerminalReceipt(TerminalReceiptState.ABSENT, item_id, generation)
    return receipt


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_terminal_root(root: Path) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    _fsync_directory(root.parent)
    return root


def _ensure_terminal_receipts_dir(root: Path, digest: str) -> Path:
    root = Path(root)
    directory = _terminal_receipts_dir(root)
    try:
        directory.mkdir()
    except FileExistsError:
        if not directory.is_dir():
            raise
    else:
        _fsync_directory(root)
    shard = _terminal_receipt_shard(root, digest)
    try:
        shard.mkdir()
    except FileExistsError:
        if not shard.is_dir():
            raise
    else:
        _fsync_directory(directory)
    return shard


def record_terminal_receipt(
    root: Path,
    item_id: str,
    disposition: Union[TerminalDisposition, str],
    generation: int = 0,
    now: Optional[float] = None,
    ttl_seconds: float = TERMINAL_RECEIPT_TTL_S,
    content_digest: Optional[str] = None,
) -> TerminalReceipt:
    """Persist one terminal outcome, replacing it for newly delivered content."""
    _terminal_identity(item_id, generation)
    _validate_content_digest(content_digest)
    try:
        terminal = TerminalDisposition(disposition)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid terminal disposition: {disposition!r}") from exc
    recorded_at, ttl = _terminal_clock_and_ttl(now, ttl_seconds)
    root = Path(root)
    digest = _terminal_digest(item_id, generation)
    shard_name = digest[:2]
    path = _terminal_receipt_shard(root, digest) / f"{digest}.json"
    encoded = _terminal_bytes(
        _terminal_payload(item_id, generation, terminal, recorded_at, content_digest))
    if len(encoded) > TERMINAL_RECEIPT_MAX_BYTES:
        raise ValueError("terminal receipt exceeds the durable record size limit")
    capacity = _terminal_shard_capacity(
        shard_name, TERMINAL_RECEIPT_MAX_RECORDS)
    if capacity == 0:
        return _unknown_terminal(item_id, generation)
    _ensure_terminal_root(root)
    with _item_lock(root, _terminal_shard_lock_id(shard_name)):
        replace_existing = False
        current = _read_terminal_path(path, item_id, generation)
        if current.state is TerminalReceiptState.UNKNOWN:
            _cleanup_terminal_receipt_shard_locked(
                root, shard_name, recorded_at, ttl, capacity,
                protected_name=path.name)
            return current
        if current.state is TerminalReceiptState.TERMINAL:
            if recorded_at - current.recorded_at < ttl:
                _cleanup_terminal_receipt_shard_locked(
                    root, shard_name, recorded_at, ttl, capacity,
                    protected_name=path.name)
                if (content_digest is None
                        or current.content_digest == content_digest):
                    return current
                replace_existing = True
            else:
                try:
                    path.unlink()
                    _fsync_directory(path.parent)
                except FileNotFoundError:
                    pass
                except OSError:
                    return _unknown_terminal(item_id, generation)
        if not replace_existing:
            cleanup = _cleanup_terminal_receipt_shard_locked(
                root, shard_name, recorded_at, ttl, max(0, capacity - 1))
            if cleanup.incomplete or cleanup.kept >= capacity:
                return _unknown_terminal(item_id, generation)
        directory = _ensure_terminal_receipts_dir(root, digest)
        fd, tmp_name = tempfile.mkstemp(dir=str(directory),
                                        prefix=f".{digest}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(encoded)
                fh.flush()
                os.fsync(fh.fileno())
            if replace_existing:
                os.replace(str(tmp), str(path))
            else:
                try:
                    os.link(str(tmp), str(path))
                except FileExistsError:
                    return _read_terminal_path(path, item_id, generation)
            _fsync_directory(directory)
            return TerminalReceipt(TerminalReceiptState.TERMINAL, item_id,
                                   generation, terminal, recorded_at,
                                   content_digest)
        finally:
            tmp.unlink(missing_ok=True)
            _fsync_directory(directory)


def _terminal_filename_digest(path: Path) -> Optional[str]:
    name = path.name
    if len(name) != 69 or not name.endswith(".json"):
        return None
    digest = name[:-5]
    return digest if all(c in "0123456789abcdef" for c in digest) else None


def _terminal_temp_digest(path: Path) -> Optional[str]:
    parts = path.name.split(".")
    if len(parts) != 4 or parts[0] or parts[-1] != "tmp":
        return None
    digest = parts[1]
    if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
        return None
    return digest


def _terminal_shard_lock_id(shard_name: str) -> str:
    return f"terminal-receipt-shard:{shard_name}"


def _terminal_shard_capacity(shard_name: str, max_records: int) -> int:
    quotient, remainder = divmod(max_records, TERMINAL_RECEIPT_SHARDS)
    return quotient + (int(shard_name, 16) < remainder)


def _same_stat(path: Path, observed) -> bool:
    try:
        current = path.lstat()
    except OSError:
        return False
    return (current.st_dev, current.st_ino) == (observed.st_dev, observed.st_ino)


def _cleanup_terminal_receipt_shard_locked(
    root: Path,
    shard_name: str,
    clock: float,
    ttl: float,
    max_records: int,
    protected_name: Optional[str] = None,
    scan_all: bool = False,
) -> TerminalReceiptCleanup:
    directory = _terminal_receipts_dir(root) / shard_name
    scan_limit = max_records + TERMINAL_RECEIPT_SWEEP_BATCH
    paths = []
    incomplete = False
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not scan_all and len(paths) >= scan_limit:
                    incomplete = True
                    break
                paths.append(Path(entry.path))
    except FileNotFoundError:
        return TerminalReceiptCleanup()
    except OSError:
        return TerminalReceiptCleanup(unknown=1, incomplete=True)

    expired = overflow = stale_temps = unknown = 0
    survivors = []
    changed = False
    for path in paths:
        temp_digest = _terminal_temp_digest(path)
        if temp_digest is not None and temp_digest[:2] == shard_name:
            try:
                observed = path.lstat()
                if _same_stat(path, observed):
                    path.unlink()
                    stale_temps += 1
                    changed = True
            except FileNotFoundError:
                pass
            except OSError:
                unknown += 1
                incomplete = True
            continue
        digest = _terminal_filename_digest(path)
        if digest is None or digest[:2] != shard_name:
            continue
        try:
            observed = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            unknown += 1
            incomplete = True
            continue
        result = _read_terminal_path(path)
        if result.state is TerminalReceiptState.UNKNOWN:
            unknown += 1
            recorded_at = observed.st_mtime
            indeterminate = True
        elif result.state is TerminalReceiptState.TERMINAL:
            recorded_at = result.recorded_at
            indeterminate = False
        else:
            continue
        protected = indeterminate or path.name == protected_name
        if not protected and clock - recorded_at >= ttl and _same_stat(path, observed):
            try:
                path.unlink()
                expired += 1
                changed = True
            except FileNotFoundError:
                pass
            except OSError:
                unknown += 1
                survivors.append((recorded_at, path.name, path, digest, observed, protected))
            continue
        survivors.append((recorded_at, path.name, path, digest, observed, protected))

    remove_count = max(0, len(survivors) - max_records)
    removable = sorted(entry for entry in survivors if not entry[-1])
    for _recorded_at, _name, path, _digest, observed, _protected in removable[:remove_count]:
        if not _same_stat(path, observed):
            continue
        try:
            path.unlink()
            overflow += 1
            changed = True
        except FileNotFoundError:
            pass
        except OSError:
            unknown += 1
    if changed:
        _fsync_directory(directory)
    kept = max(0, len(survivors) - overflow)
    incomplete = incomplete or kept > max_records
    return TerminalReceiptCleanup(
        expired, overflow, stale_temps, unknown, kept, incomplete)


def cleanup_terminal_receipts(
    root: Path,
    ttl_seconds: float = TERMINAL_RECEIPT_TTL_S,
    max_records: int = TERMINAL_RECEIPT_MAX_RECORDS,
    *,
    now: Optional[float] = None,
) -> TerminalReceiptCleanup:
    """Expire receipts and enforce bounded per-shard quotas conservatively."""
    clock, ttl = _terminal_clock_and_ttl(now, ttl_seconds)
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 0:
        raise ValueError("max_records must be a non-negative integer")
    root = Path(root)
    directory = _terminal_receipts_dir(root)
    try:
        directory.lstat()
    except FileNotFoundError:
        return TerminalReceiptCleanup()
    except OSError:
        return TerminalReceiptCleanup(unknown=1, incomplete=True)
    if not directory.is_dir():
        return TerminalReceiptCleanup(unknown=1, incomplete=True)
    expired = overflow = stale_temps = unknown = kept = 0
    incomplete = False
    for index in range(TERMINAL_RECEIPT_SHARDS):
        shard_name = f"{index:02x}"
        shard = directory / shard_name
        try:
            shard.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            unknown += 1
            incomplete = True
            continue
        capacity = _terminal_shard_capacity(shard_name, max_records)
        with _item_lock(root, _terminal_shard_lock_id(shard_name)):
            result = _cleanup_terminal_receipt_shard_locked(
                root, shard_name, clock, ttl, capacity, scan_all=True)
        expired += result.expired
        overflow += result.overflow
        stale_temps += result.stale_temps
        unknown += result.unknown
        kept += result.kept
        incomplete = incomplete or result.incomplete
    return TerminalReceiptCleanup(
        expired, overflow, stale_temps, unknown, kept, incomplete)
