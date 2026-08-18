#!/usr/bin/env python3
"""Design C prototype: B's single-object, move-based lifecycle + A's ownership
transition discipline. Not production code; exp/ branch only.

    ABSENT --publish--> READY(gen) --claim--> HELD(gen, token) --finalize--> DONE
                          ^                       |
                          +----- recover ---------+   (dead owner, gen+1)
                          +----- force_requeue ---+   (administrative, gen+1)

Two things the A/B round separated, and C keeps separate:

  durable ownership IDENTITY   the object's own name IS the claim record:
                               <key>~<gen>~<worker>~<pid>~<birth>. Crash-
                               inspectable, survives every process, and needs
                               no second per-item file.
  ownership TRANSITION control every arrow above runs inside the item's mutex:
                               observe -> validate -> exactly ONE atomic
                               rename. Nodes are unlocked; arrows are locked.

Because each edge verifies its precondition under the same mutex that performs
the mutation, check-then-act INSIDE an edge is legitimate — that is C's whole
thesis, and it is why the destination of every transition is provably fresh
(so every edge is a plain rename: one linearization point, no link+unlink pair,
no recheck-after-link, no withdraw).

Long I/O stays OUTSIDE the mutex: the payload write in publish() and the
delivery between claim() and finalize() are not ownership transitions.

PRECONDITION, stated rather than discovered later: C's serialization is
HOST-LOCAL. flock is one kernel's mutex, so every ownership mutator must run on
the machine that owns `root`. Cross-host claim assignment (the lease-scheduler
direction the core heartbeat anticipates) requires a different mutex; adding a
second host without one degrades C silently to exactly the compare-then-act
races it exists to eliminate — every test green on one host while two hosts race.

safe_key/_safe_component/_d are duplicated from designB.py on purpose: the two
prototypes must be measurable in isolation, so neither imports the other.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import outbox as ob  # ALIVE/DEAD/UNKNOWN process oracle  # noqa: E402

TMP, READY, INFLIGHT, ARCHIVE, PARKED, LOCKS = (
    "tmp", "ready", "inflight", "archive", "undelivered", "locks")
SEP = "~"

# Fixed stripe pool: mutual exclusion with a BOUNDED object count. Per-item
# lock files were the unbounded-inode shape measured on this host; a fixed pool
# also removes any need to prove unlink-vs-holder safety, because nothing is
# ever unlinked. Cost: bounded false contention between distinct items sharing
# a stripe, over a metadata-only critical section.
STRIPES = int(os.environ.get("C_LOCK_STRIPES", "256"))

ABSENT, HELD, AVAILABLE = "ABSENT", "HELD", "READY"


class OutboxConfigError(RuntimeError):
    """Non-race filesystem failure (permissions, cross-device, ...): the root is
    misconfigured. Raised, never folded into a lost-race None/False."""


class InvariantError(RuntimeError):
    """Two live objects for one item, or an unparsable one. Unreachable while
    every transition holds the item's mutex; it is the detector that says so."""


class ItemState(NamedTuple):
    state: str                  # ABSENT | READY | HELD
    gen: int                    # -1 when ABSENT
    token: str | None           # HELD: the object name, which IS the token
    worker: str | None
    path: Path | None


def _d(root, name) -> Path:
    p = Path(root) / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_key(item_id: str) -> str:
    """Readable stub + digest; injective, and free of SEP and path chars."""
    stub = "".join(c if (c.isalnum() or c in "-._") else "_" for c in item_id)[:60]
    return f"{stub}={hashlib.sha256(item_id.encode('utf-8')).hexdigest()[:16]}"


def _safe_component(s: str) -> str:
    out = "".join(c if (c.isalnum() or c in "-._") else "_" for c in s)[:40]
    if not out:
        raise ValueError("empty component")
    return out


def stripe_index(key: str) -> int:
    """Derived from the safe_key, not the raw id: finalize()/recover() hold only
    a token, and the key is the one component a token can always yield."""
    return int.from_bytes(
        hashlib.sha256(key.encode("utf-8")).digest()[:4], "big") % STRIPES


@contextlib.contextmanager
def key_lock(root, key: str):
    """Host-local mutual exclusion for one item's ownership transitions."""
    p = _d(root, LOCKS) / f"{stripe_index(key):04d}.lock"
    try:
        fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise OutboxConfigError(f"lock {p}: {e}") from e
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                    # close releases the flock


@contextlib.contextmanager
def item_lock(root, item_id: str):
    with key_lock(root, safe_key(item_id)):
        yield


def _locate(root, key: str):
    """The item's single live object, or None. Raises if there are two: with
    every edge serialized and single-rename, that state cannot be produced."""
    hits = []
    for d in (READY, INFLIGHT):
        p = Path(root) / d
        if not p.exists():
            continue
        for f in p.iterdir():
            if f.name.split(SEP)[0] == key:
                hits.append((d, f))
    if len(hits) > 1:
        raise InvariantError(
            f"{key}: {len(hits)} live objects "
            f"{sorted(f'{d}/{f.name}' for d, f in hits)}")
    return hits[0] if hits else None


def _state_of(root, key: str) -> ItemState:
    hit = _locate(root, key)
    if hit is None:
        return ItemState(ABSENT, -1, None, None, None)
    d, f = hit
    parts = f.name.split(SEP)
    want = 2 if d == READY else 5
    if len(parts) != want or not parts[1].isdigit():
        raise InvariantError(f"unparsable {d}/{f.name}")
    gen = int(parts[1])
    if d == READY:
        return ItemState(AVAILABLE, gen, None, None, f)
    return ItemState(HELD, gen, f.name, parts[2], f)


def observe(root, item_id: str) -> ItemState:
    """Read the item's state. Meaningful for a DECISION only while holding the
    item's lock — outside it this is a snapshot, and acting on it is the
    compare-then-act defect class C exists to remove."""
    return _state_of(root, safe_key(item_id))


def _rename(src: Path, dst: Path) -> None:
    """The single linearization point of every edge. Callers verify the
    destination is fresh under the lock, so there is no EEXIST case."""
    try:
        os.rename(str(src), str(dst))
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise InvariantError(f"{src} vanished under the item lock") from e
        raise OutboxConfigError(f"{src} -> {dst}: {e}") from e


def publish(root, item_id: str, body: str = "") -> bool:
    """ABSENT -> READY(0). False = this id is already live in ANY state.

    The payload write is data I/O, so it happens before the lock; only the
    namespace transition is serialized. ABSENT verified under the lock means
    ready/<key>~0 cannot pre-exist, so publish is one plain rename."""
    key = safe_key(item_id)
    tmp = _d(root, TMP) / f"{key}{SEP}{os.getpid()}{SEP}{time.time_ns()}"
    tmp.write_text(body, encoding="utf-8")
    with key_lock(root, key):
        if _state_of(root, key).state is not ABSENT:
            tmp.unlink(missing_ok=True)
            return False
        _rename(tmp, _d(root, READY) / f"{key}{SEP}0")
    return True


def claim(root, item_id: str, worker: str):
    """READY(gen) -> HELD(gen, token). None = not claimable right now.

    The returned token is the object's name: item + generation + worker + pid +
    incarnation. That IS the durable ownership record — there is no second
    file to keep consistent with it."""
    key = safe_key(item_id)
    with key_lock(root, key):
        st = _state_of(root, key)
        if st.state is not AVAILABLE:
            return None
        ident = ob.process_identity(os.getpid())
        token = SEP.join((key, str(st.gen), _safe_component(worker),
                          str(os.getpid()), str(ident.start_usec)))
        _rename(st.path, _d(root, INFLIGHT) / token)
        return token


def finalize(root, token: str, terminal: str = ARCHIVE) -> bool:
    """HELD -> DONE. False = the token is not the current ownership.

    The full identity (item, generation, worker, pid, incarnation) is verified
    at the same serialization point as the move, so a holder that returns after
    a force_requeue or recover cannot finalize its successor's claim: its
    generation no longer matches. This is what makes A's three release-side
    counterexamples unreachable by construction rather than by window
    narrowing."""
    key = token.split(SEP)[0]
    with key_lock(root, key):
        st = _state_of(root, key)
        if st.state is not HELD or st.token != token:
            return False
        # Check-then-act is legitimate here: we hold the item's mutex, which is
        # exactly the property that makes a terminal name provably fresh.
        dst = _d(root, terminal) / f"{token}{SEP}{time.time_ns()}"
        while dst.exists():
            dst = dst.with_name(f"{token}{SEP}{time.time_ns()}")
        _rename(st.path, dst)
        return True


def recover(root):
    """HELD(dead owner) -> READY(gen+1). Returns the keys re-armed.

    The liveness decision and the transition share one critical section, so the
    owner cannot become live, finalize, or be forced between them. There is no
    recheck-after-link and no withdraw path: the destination is provably fresh
    because HELD was verified under the same lock."""
    moved = []
    for f in sorted(_d(root, INFLIGHT).iterdir()):
        parts = f.name.split(SEP)
        if len(parts) != 5 or not parts[1].isdigit():
            continue
        key, gen_s, _worker, pid_s, birth = parts
        if not pid_s.isdigit():
            continue
        with key_lock(root, key):
            st = _state_of(root, key)
            if st.state is not HELD or st.token != f.name:
                continue                # already transitioned by someone else
            ident = ob.process_identity(int(pid_s))
            if ident.state is not ob.OwnerState.DEAD:
                continue                # ALIVE and UNKNOWN are never touched
            if ident.start_usec is not None and str(ident.start_usec) != birth:
                continue                # pid reuse: a different incarnation
            _rename(st.path, _d(root, READY) / f"{key}{SEP}{int(gen_s) + 1}")
            moved.append(key)
    return moved


def force_requeue(root, item_id: str) -> bool:
    """Administrative requeue (A's force-release), as one more edge through the
    same mutex: whatever holds the item at the lock instant loses it, and the
    generation bump makes that loss verifiable by the old holder.

    Its observation instant IS its mutation instant, so — unlike A's original
    unconditional release — it cannot destroy a claim created after it
    completes. True = nobody holds the item afterwards."""
    key = safe_key(item_id)
    with key_lock(root, key):
        st = _state_of(root, key)
        if st.state is not HELD:
            return True                 # postcondition already holds
        _rename(st.path, _d(root, READY) / f"{key}{SEP}{st.gen + 1}")
        return True


def holder(root, item_id: str):
    """The current owner's worker, or None. Deterministic without a tie-break:
    an item has at most ONE live object, so there is no set to order."""
    return observe(root, item_id).worker


def cleanup(root, max_age_s: float, now: float | None = None) -> int:
    """Bound on-disk state: prune terminal records and sweep tmp debris. The
    lock pool is deliberately NOT swept — it is a fixed 256-object namespace,
    and unlinking a lock file is the holder/waiter hazard the pool avoids."""
    now = time.time() if now is None else now
    pruned = 0
    for d in (ARCHIVE, PARKED, TMP):
        for f in list(_d(root, d).iterdir()):
            try:
                if now - f.stat().st_mtime > max_age_s:
                    f.unlink(missing_ok=True)
                    pruned += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                raise OutboxConfigError(f"cleanup {f}: {e}") from e
    return pruned
