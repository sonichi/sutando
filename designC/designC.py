#!/usr/bin/env python3
"""Design C prototype: Design B's namespace-as-state, serialized by a
host-local flock, with a claim-UNIQUE token.

C is B (designB-eval/designB.py) plus exactly two changes:

  1. token = key~worker~pid~birth~GEN. B's key~worker~pid~birth is unique per
     claimer INCARNATION, never per claim -- complete -> republish -> reclaim
     by the same live process reproduces the byte-identical name, so a stale
     finalize from the first claim epoch completes the second claim's work.
     GEN is a per-claim nonce, so no two claims can ever share a name.
  2. every transition runs inside outbox._item_lock (the striped pool that
     landed on main in #3001), so observe-then-act sequences -- publish's
     inflight probe, recover's re-arm -- are serialized rather than raced.

Nothing else moves: the directory layout, the atomicity argument for each
rename, quarantine-on-collision and the retention sweep are B's, unchanged,
so a measurement difference against B attributes to those two changes.

Scope invariants, stated because a striped lock pool READS like a general
mutex and is not one:
  * the flock is HOST-LOCAL; durable ownership identity is global. Two hosts
    sharing a root are excluded by the durable record, never by this lock.
  * serialization removes the compare-then-act window. It does NOT resolve
    the liveness oracle: process_identity still answers ALIVE/DEAD/UNKNOWN.
  * every directory here must sit on ONE filesystem (rename/link are EXDEV).
  * activate_lock_striping's contract is whole-engine quiescence. C assumes a
    greenfield root -- no pre-striping holder can exist -- and nothing here
    licenses activating a striped pool on a live legacy store.
  * LOCK_STRIPES is a migration, not a knob: changing it remaps item->stripe,
    so mixed-value processes stop mutually excluding.
"""
from __future__ import annotations

import errno
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import outbox as ob  # ALIVE/DEAD/UNKNOWN oracle + striped item lock  # noqa: E402

TMP, READY, INFLIGHT, ARCHIVE, PARKED = "tmp", "ready", "inflight", "archive", "undelivered"
SEP = "~"
TOKEN_PARTS = 5

_INITIALIZED: set[str] = set()

ABSENT, HELD, AVAILABLE = "ABSENT", "HELD", "READY"


class OutboxConfigError(RuntimeError):
    """Non-race filesystem failure (permissions, cross-device, ...): the root
    is misconfigured. Raised, never folded into a lost-race None/False."""


class NotInitialized(RuntimeError):
    """A mutating entry point ran against a root init() never prepared.
    Lazy activation is the proven-unsound path (see _item_lock); raising is
    the only sound alternative to it."""


class InvariantError(RuntimeError):
    """Two simultaneous HELD tokens for one key. ready/inflight coexistence
    is NOT this error: recover's collision leg quarantines it by design."""


def _d(root, name):
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
    """CONSTRUCTOR-TIME activation — the only sound placement. Lazy claim-path
    activation is unsound under same-process concurrency, and unfixably so:
    (a) the fence tmp is pid-suffixed — thread-shared — so racing activators
    crash on os.replace (ClaimMachine CE 2026-08-18, 2-op schedule); (b) worse,
    ob._stripe_mode negatively memoizes per process BY DESIGN (no mid-flight
    namespace flip), so a losing thread keeps the cached "unstriped" verdict
    and locks per-item files while the winner locks stripes — NO mutual
    exclusion, silently. init() runs dirs + activate_lock_striping once,
    before any thread's first fence read for the root; every mutating entry
    point raises NotInitialized rather than activating lazily."""
    for name in (TMP, READY, INFLIGHT, ARCHIVE, PARKED):
        _d(root, name)
    ob.activate_lock_striping(Path(root))
    _INITIALIZED.add(os.path.realpath(str(root)))


def _item_lock(root, key: str):
    """Serialize one item's transitions. Keyed on safe_key, NOT the raw id:
    recover only ever sees the key, and two spellings would stripe apart."""
    if os.path.realpath(str(root)) not in _INITIALIZED:
        raise NotInitialized(f"init({root!s}) must run before any mutation")
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


def publish(root, item_id: str, body: str = "") -> bool:
    """True = published. False = this id is already live (ready/ OR in flight).
    Unlike B, the inflight probe and the link run under the item lock, so the
    probe is not a compare-then-act: no claim can intervene between them."""
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
    earlier claim epoch can never name this one."""
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
    now includes a stale token whose epoch ended: its name is not reachable."""
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
    if os.path.realpath(str(root)) not in _INITIALIZED:
        raise NotInitialized(f"init({root!s}) must run before any mutation")
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
            continue
        with _item_lock(root, key):
            if not f.exists():             # a racer settled it first
                continue
            # never re-arm past a LIVE same-key token: a duplicate copy must
            # quarantine, not deliver.
            others = [t for t in _inflight_tokens(root, key) if t.name != f.name]
            if others:
                _quarantine(f, root, key, "live-holder", pid_s)
                continue
            # ready/<key> must be create-if-absent (a re-publish may occupy
            # it), so this one transition stays link+unlink.
            if _move(f, _d(root, READY) / key):
                moved.append(key)
            elif f.exists():               # ready/ occupied
                _quarantine(f, root, key, "collision", pid_s)
    return moved


def cleanup(root, max_age_s: float, now: float | None = None) -> int:
    """Bound on-disk state: prune archive/ + undelivered/ entries older than
    max_age_s, and sweep tmp/ debris (torn publishes) on the same clock."""
    if os.path.realpath(str(root)) not in _INITIALIZED:
        raise NotInitialized(f"init({root!s}) must run before any mutation")
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


class ItemState(NamedTuple):
    state: str                  # ABSENT | READY | HELD
    gen: str | None             # per-claim nonce from the token; None otherwise
    token: str | None           # HELD: the object name, which IS the token
    worker: str | None
    path: Path | None


def _locate(root, key: str):
    """The item's operative live object, or None. ready/inflight coexistence
    and dead-ghost-beside-live-claim are ANTICIPATED intermediates (recover's
    legs quarantine them), so neither raises: a LIVE token wins, else the most
    recent dead incarnation (max (birth, name) — deterministic, never
    iteration order), else ready/. Only two simultaneous LIVE holders raise:
    that state has no producer."""
    live, dead = [], []
    for f in _inflight_tokens(root, key):
        parts = f.name.split(SEP)
        if len(parts) != TOKEN_PARTS:
            continue
        try:
            ident = ob.process_identity(int(parts[2]))
        except ValueError:
            continue
        if (ident.state is not ob.OwnerState.DEAD
                and str(ident.start_usec) == parts[3]):
            live.append(f)
        else:
            dead.append((int(parts[3]) if parts[3].isdigit() else -1,
                         f.name, f))
    if len(live) > 1:
        raise InvariantError(
            f"{key}: {len(live)} simultaneous LIVE holders "
            f"{sorted(f.name for f in live)}")
    if live:
        return INFLIGHT, live[0]
    if dead:
        return INFLIGHT, max(dead)[2]
    ready = Path(root) / READY / key
    if ready.exists():
        return READY, ready
    return None


def _state_of(root, key: str) -> ItemState:
    hit = _locate(root, key)
    if hit is None:
        return ItemState(ABSENT, None, None, None, None)
    d, f = hit
    if d == READY:
        return ItemState(AVAILABLE, None, None, None, f)
    parts = f.name.split(SEP)
    if len(parts) != TOKEN_PARTS:
        raise InvariantError(f"unparsable {d}/{f.name}")
    return ItemState(HELD, parts[4], f.name, parts[1], f)


def observe(root, item_id: str) -> ItemState:
    """Read the item's state. Meaningful for a DECISION only while holding the
    item's lock — outside it this is a snapshot, and acting on it is the
    compare-then-act defect class C exists to remove."""
    return _state_of(root, safe_key(item_id))


def force_requeue(root, item_id: str) -> bool:
    """Administrative requeue: whatever holds the item at the lock instant
    loses it. ready/<key> must be create-if-absent (a re-publish may occupy
    it), so the transfer is _move, not rename. True = nobody holds after."""
    key = safe_key(item_id)
    with _item_lock(root, key):
        st = _state_of(root, key)
        if st.state is not HELD:
            return True                 # postcondition already holds
        if not _move(st.path, _d(root, READY) / key) and st.path.exists():
            _quarantine(st.path, root, key, "collision", "forced")
        return True


def holder(root, item_id: str):
    """The current holder's worker, or None: at most one live token exists
    (two raise in _locate), so there is no set to order."""
    return _state_of(root, safe_key(item_id)).worker
