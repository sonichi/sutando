#!/usr/bin/env python3
"""Design C, unified: one prototype replacing designC-eval/ and
designC-eval-harness/, built on the substrates the owner selected and the
namespace 001 settled.

    ABSENT --publish--> READY --claim--> INFLIGHT(token) --complete--> terminal
                          ^                    |
                          +---- recover -------+   (DEAD owner only)
                          +---- force_requeue -+   (administrative)

SUBSTRATES (decided, not open):
  lock      outbox's striped `_item_lock` pool. Not a private flock: one
            implementation, one activation contract, one place to fix.
  transfer  B's `_move` (link + unlink) wherever the destination is a SHARED
            name, i.e. every edge landing in ready/<key>. Plain `os.rename`
            only where the destination is provably unique -- inflight/<token>
            and terminal/<key~rest> -- because the claim generation makes
            those names unreachable by any other claim.

WHY BOTH, when a single-owner control showed either alone suffices: they fail
differently. `os.link` refuses a second live slot STRUCTURALLY, so bare
ready/<key> holds one-live-slot-per-item even with the lock neutered or
absent; the mutex additionally protects COMPOUND transitions (publish's
probe-then-link, recover's re-arm) that no single rename covers. Measured:
under a neutered lock this shape refuses the loser with EEXIST, while a
generation-qualified ready namespace admits two live generations of one item.
Defense in depth, with the layers doing different jobs.

ACTIVATION IS CONSTRUCTOR-TIME, AND THAT IS A CORRECTNESS CONSTRAINT.
`ob._stripe_mode` negatively memoizes per process by design (no mid-flight
namespace flip). A thread that reads the fence before striping is activated
caches "unstriped" forever and then locks per-item files while a sibling locks
stripes -- no mutual exclusion, silently, with every test still green. So
striping is activated once in `init()` before any thread touches the root, and
the mutating entry points REFUSE an uninitialized root rather than activating
it lazily. A loud error replaces a silent loss of exclusion.
"""
from __future__ import annotations

import errno
import hashlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import outbox as ob  # ALIVE/DEAD/UNKNOWN oracle + striped item lock  # noqa: E402

TMP, READY, INFLIGHT, ARCHIVE, PARKED = "tmp", "ready", "inflight", "archive", "undelivered"
SEP = "~"
TOKEN_PARTS = 5

ABSENT, AVAILABLE, HELD = "ABSENT", "READY", "HELD"

_INIT: set[str] = set()
_INIT_GUARD = threading.Lock()


class OutboxConfigError(RuntimeError):
    """Non-race filesystem failure (permissions, cross-device, ...): the root
    is misconfigured. Raised, never folded into a lost-race None/False."""


class InvariantError(RuntimeError):
    """A state the design proves unreachable was nevertheless observed."""


class NotInitialized(RuntimeError):
    """A mutating call reached a root that `init()` never activated."""


class ItemState(NamedTuple):
    state: str
    token: str | None
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


def _generation() -> str:
    """Per-CLAIM nonce. Clock alone is not enough: two claims can land in one
    nanosecond, and a nonce must not depend on clock resolution to be unique."""
    return f"{time.time_ns():x}{os.urandom(6).hex()}"


def init(root) -> None:
    """Activate striping for `root` BEFORE any thread reads its fence. Must be
    called once per root per process; idempotent, and safe to race."""
    rk = os.path.realpath(str(root))
    with _INIT_GUARD:
        if rk in _INIT:
            return
        for d in (TMP, READY, INFLIGHT, ARCHIVE, PARKED):
            _d(root, d)
        ob.activate_lock_striping(Path(root))
        _INIT.add(rk)


def _item_lock(root, key: str):
    """Serialize one item's transitions. Keyed on safe_key, NOT the raw id:
    recover only ever sees the key, and two spellings would stripe apart."""
    if os.path.realpath(str(root)) not in _INIT:
        raise NotInitialized(
            f"init({root!r}) was never called; activating striping now would "
            f"race any thread that has already cached the unstriped verdict")
    return ob._item_lock(Path(root), key)


def _race_lost(e: OSError) -> bool:
    return e.errno in (errno.ENOENT, errno.EEXIST)


def _move(src: Path, dst: Path) -> bool:
    """Atomic create-if-absent transfer: link then unlink. True = we moved it.
    False = lost a race (src gone, or dst occupied). Config errors raise."""
    try:
        os.link(str(src), str(dst))
    except OSError as e:
        if _race_lost(e):
            return False
        raise OutboxConfigError(f"{src} -> {dst}: {e}") from e
    try:
        os.unlink(str(src))
    except FileNotFoundError:
        pass  # a racer unlinked the source after our link; the transfer stands
    except OSError as e:
        raise OutboxConfigError(f"unlink {src}: {e}") from e
    return True


def _quarantine(src: Path, root, item_key: str, reason: str, token: str) -> None:
    dst = _d(root, PARKED) / f"{item_key}{SEP}{reason}{SEP}{token}"
    if not _move(src, dst):
        # even the quarantine slot is taken: uniquify by nanotime; a further
        # loss means the source vanished, which satisfies exactly-one-copy.
        _move(src, dst.with_name(dst.name + f"{SEP}{time.time_ns()}"))


def _inflight_tokens(root, key: str):
    d = Path(root) / INFLIGHT
    if not d.exists():
        return []
    return [f for f in d.iterdir() if f.name.split(SEP)[0] == key]


def _state_of(root, key: str) -> ItemState:
    """The item's live state; a held token outranks a ready copy.

    ready/ and inflight/ CAN both hold this key -- recover's collision leg
    exists precisely to quarantine that state -- so coexistence is reported,
    not rejected. Only two simultaneous holders are unreachable: claim
    consumes ready/ with a single rename, so at most one racer wins it."""
    ready = Path(root) / READY / key
    held = _inflight_tokens(root, key)
    if len(held) > 1:
        raise InvariantError(f"{key}: {len(held)} inflight tokens {[f.name for f in held]}")
    if held:
        parts = held[0].name.split(SEP)
        return ItemState(HELD, held[0].name, parts[1], held[0])
    if ready.exists():
        return ItemState(AVAILABLE, None, None, ready)
    return ItemState(ABSENT, None, None, None)


def observe(root, item_id: str) -> ItemState:
    """Read the item's state. Meaningful for a DECISION only while holding the
    item's lock -- outside it this is a snapshot, and acting on it is the
    compare-then-act defect class C exists to remove."""
    return _state_of(root, safe_key(item_id))


def publish(root, item_id: str, body: str = "") -> bool:
    """True = published. False = this id is already live (ready/ OR in flight).
    The inflight probe and the link run under the item lock, so the probe is
    not a compare-then-act: no claim can intervene between them."""
    key = safe_key(item_id)
    with _item_lock(root, key):
        if _inflight_tokens(root, key):
            return False
        tmp = _d(root, TMP) / f"{key}{SEP}{os.getpid()}{SEP}{time.time_ns()}"
        tmp.write_text(body, encoding="utf-8")
        dst = _d(root, READY) / key
        try:
            os.link(str(tmp), str(dst))        # atomic create-if-absent
        except OSError as e:
            tmp.unlink(missing_ok=True)
            if e.errno == errno.EEXIST:
                return False
            raise OutboxConfigError(f"publish {item_id!r}: {e}") from e
        tmp.unlink(missing_ok=True)
        return True


def claim(root, item_id: str, worker: str):
    """Claim by moving the source; exactly one racer wins. None = lost/absent.
    The generation makes the destination unique per CLAIM, so a token from an
    earlier claim epoch can never name this one (CE-6)."""
    key = safe_key(item_id)
    with _item_lock(root, key):
        ident = ob.process_identity(os.getpid())
        token = SEP.join((key, _safe_component(worker), str(os.getpid()),
                          str(ident.start_usec), _generation()))
        try:
            os.rename(str(_d(root, READY) / key), str(_d(root, INFLIGHT) / token))
        except OSError as e:
            if e.errno == errno.ENOENT:
                return None
            raise OutboxConfigError(f"claim {item_id!r}: {e}") from e
        return token


def complete(root, token: str, terminal: str = ARCHIVE) -> bool:
    """Finish a claim. False = this token does not name a live claim, which
    includes a stale token whose epoch ended: its name is not reachable."""
    key, rest = token.split(SEP, 1)
    with _item_lock(root, key):
        dst = _d(root, terminal) / f"{key}{SEP}{rest}"
        try:
            os.rename(str(_d(root, INFLIGHT) / token), str(dst))
        except OSError as e:
            if e.errno == errno.ENOENT:
                return False               # src gone: not ours to finish
            raise OutboxConfigError(f"complete {token!r}: {e}") from e
        return True


def recover(root):
    """Return DEAD owners' items to ready/. UNKNOWN/ALIVE are never touched."""
    moved = []
    for f in sorted(_d(root, INFLIGHT).iterdir()):
        parts = f.name.split(SEP)
        if len(parts) != TOKEN_PARTS:
            continue
        key, _worker, pid_s, birth, _gen = parts
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        ident = ob.process_identity(pid)
        if ident.state is not ob.OwnerState.DEAD:
            continue
        if ident.start_usec is not None and str(ident.start_usec) != birth:
            continue                       # pid reuse: a different incarnation
        with _item_lock(root, key):
            if not f.exists():             # a racer settled it first
                continue
            # never re-arm past a LIVE same-key token: a duplicate copy must
            # quarantine, not deliver.
            others = [t for t in _inflight_tokens(root, key) if t.name != f.name]
            if others:
                _quarantine(f, root, key, "live-holder", pid_s)
                continue
            # ready/<key> is a SHARED name a re-publish may occupy, so this
            # edge is create-if-absent, never a clobbering rename.
            if _move(f, _d(root, READY) / key):
                moved.append(key)
            elif f.exists():               # ready/ occupied
                _quarantine(f, root, key, "collision", pid_s)
    return moved


def force_requeue(root, item_id: str) -> bool:
    """Administrative release. Its observation instant IS its mutation instant,
    so unlike an unconditional release it cannot destroy a claim created after
    it began. True = nobody holds the item afterwards."""
    key = safe_key(item_id)
    with _item_lock(root, key):
        st = _state_of(root, key)
        if st.state is not HELD:
            return True                    # postcondition already holds
        if _move(st.path, _d(root, READY) / key):
            return True
        _quarantine(st.path, root, key, "collision", "force")
        return True


def cleanup(root, max_age_s: float, now: float | None = None) -> int:
    """Bound on-disk state: prune archive/ + undelivered/ entries older than
    max_age_s, and sweep tmp/ debris (torn publishes) on the same clock."""
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


def holder(root, item_id: str):
    """The current owner's worker, or None. One live object per item means
    there is no set to order and no tie-break to get wrong."""
    return _state_of(root, safe_key(item_id)).worker
