"""DesignCClaimBackend: namespace-as-state claiming behind the ClaimBackend
seam — the accepted Design C protocol (2026-08-18 pair report), productized.

The object's location IS its state and the inflight filename IS the durable
claim record: ready/<key> (bare — one live slot, structurally, via link's
EEXIST) -> inflight/<key~worker~pid~birth~generation> -> archive|undelivered.
The striped item lock serializes compound transitions (publish's probe-then-
link is its uniquely load-bearing surface); the per-claim generation makes
tokens unique per CLAIM, not per claimer incarnation (kills the CE-6
republish ABA). Dead-ghost-beside-live-claim is an ANTICIPATED intermediate:
liveness decisions count LIVE holders, never raw tokens.

Activation is constructor-time and that is a correctness constraint: the
lock-stripe fence memoizes negatively per process, so lazy activation can
silently split lockers across namespaces. init happens in __init__ under a
process-wide guard; there is no lazy path.
"""
from __future__ import annotations

import errno
import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Optional

from .. import outbox
from .contract import (BackendCapabilities, ClaimToken, CleanupReport,
                       DeliveryOutcome, RecoverReport)

TMP, READY, INFLIGHT, ARCHIVE, PARKED, ATTEMPTS = (
    "tmp", "ready", "inflight", "archive", "undelivered", "attempts")
SEP = "~"
TOKEN_PARTS = 5

_ACTIVATED: set[str] = set()
_ACTIVATE_GUARD = threading.Lock()


class OutboxConfigError(RuntimeError):
    """Non-race filesystem failure (permissions, cross-device, ...): the
    root is misconfigured. Raised, never folded into a lost-race outcome."""


class InvariantError(RuntimeError):
    """Two simultaneous LIVE holders for one key — a state with no producer.
    Raw-token plurality is NOT this error (dead ghosts are anticipated)."""


def _safe_key(item_id: str) -> str:
    stub = "".join(c if (c.isalnum() or c in "-._") else "_" for c in item_id)[:60]
    return f"{stub}={hashlib.sha256(item_id.encode('utf-8')).hexdigest()[:16]}"


def _safe_component(s: str) -> str:
    out = "".join(c if (c.isalnum() or c in "-._") else "_" for c in s)[:40]
    if not out:
        raise ValueError("empty component")
    return out


def _generation() -> str:
    """Per-CLAIM nonce; clock alone can collide within one nanosecond."""
    return f"{time.time_ns():x}{os.urandom(6).hex()}"


def _race_lost(e: OSError) -> bool:
    return e.errno in (errno.ENOENT, errno.EEXIST)


def _move(src: Path, dst: Path) -> bool:
    """Atomic create-if-absent transfer: link then unlink. True = we moved
    it; False = lost a race (src gone / dst occupied). Config errors raise."""
    try:
        os.link(str(src), str(dst))
    except OSError as e:
        if _race_lost(e):
            return False
        raise OutboxConfigError(f"{src} -> {dst}: {e}") from e
    try:
        os.unlink(str(src))
    except FileNotFoundError:
        pass
    except OSError as e:
        raise OutboxConfigError(f"unlink {src}: {e}") from e
    return True


class DesignCClaimBackend:
    """C: one live object per item, moved between state directories under a
    striped host-local lock. force-release exists as administrative requeue."""

    # The FILENAME is the record here — an archived rename carries no field
    # to hold receipt metadata, so complete() accepts and drops it.
    persists_receipt_metadata = False

    capabilities = BackendCapabilities(supports_force_release=True)

    def __init__(self, root: Path, activate: bool = False):
        self.root = Path(root)
        for name in (TMP, READY, INFLIGHT, ARCHIVE, PARKED, ATTEMPTS):
            self._d(name)
        rk = os.path.realpath(str(self.root))
        with _ACTIVATE_GUARD:
            if rk in _ACTIVATED:
                return
            # Assert, don't perform: activation is a quiescence-requiring
            # migration; only the deploy path (activate=True) may run it.
            if activate:
                outbox.activate_lock_striping(self.root)
            # _stripe_mode validates the fence (corrupt JSON / stripe-count
            # mismatch raise there); bare path-existence would accept both.
            elif not outbox._stripe_mode(self.root):
                raise RuntimeError(
                    f"outbox root {self.root} is not stripe-fenced: run "
                    "activation during a deploy window (no other consumer "
                    "of this root running), e.g. "
                    "DesignCClaimBackend(root, activate=True)")
            _ACTIVATED.add(rk)

    # ── namespace helpers ───────────────────────────────────────────────
    def _d(self, name: str) -> Path:
        p = self.root / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _lock(self, key: str):
        return outbox._item_lock(self.root, key)

    def _tokens(self, key: str) -> list[Path]:
        d = self.root / INFLIGHT
        if not d.exists():
            return []
        return [f for f in d.iterdir() if f.name.split(SEP)[0] == key]

    def _live_and_dead(self, key: str):
        """Tokens split by owner liveness. A token is LIVE only when its pid
        is not DEAD and its recorded birth matches that pid's incarnation."""
        live, dead = [], []
        for f in self._tokens(key):
            parts = f.name.split(SEP)
            if len(parts) != TOKEN_PARTS:
                continue
            try:
                ident = outbox.process_identity(int(parts[2]))
            except ValueError:
                continue
            if (ident.state is not outbox.OwnerState.DEAD
                    and str(ident.start_usec) == parts[3]):
                live.append(f)
            else:
                dead.append(f)
        if len(live) > 1:
            raise InvariantError(
                f"{key}: {len(live)} simultaneous LIVE holders "
                f"{sorted(f.name for f in live)}")
        return live, dead

    def _quarantine(self, src: Path, key: str, reason: str, tag: str) -> None:
        dst = self._d(PARKED) / f"{key}{SEP}{reason}{SEP}{tag}"
        if not _move(src, dst):
            _move(src, dst.with_name(dst.name + f"{SEP}{time.time_ns()}"))

    # ── ClaimBackend surface ────────────────────────────────────────────
    def publish(self, item_id: str, payload: bytes) -> bool:
        key = _safe_key(item_id)
        with self._lock(key):
            # RAW tokens block on purpose: a dead ghost holding the slot is
            # the verified harmless-by-construction recover-window semantics.
            if self._tokens(key):
                return False
            tmp = self._d(TMP) / f"{key}{SEP}{os.getpid()}{SEP}{time.time_ns()}"
            tmp.write_bytes(payload)
            dst = self._d(READY) / key
            try:
                os.link(str(tmp), str(dst))     # atomic create-if-absent
            except OSError as e:
                tmp.unlink(missing_ok=True)
                if e.errno == errno.EEXIST:
                    return False
                raise OutboxConfigError(f"publish {item_id!r}: {e}") from e
            tmp.unlink(missing_ok=True)
            return True

    def claim(self, item_id: str, worker: str) -> Optional[ClaimToken]:
        key = _safe_key(item_id)
        with self._lock(key):
            ident = outbox.process_identity(os.getpid())
            fname = SEP.join((key, _safe_component(worker), str(os.getpid()),
                              str(ident.start_usec), _generation()))
            try:
                os.rename(str(self._d(READY) / key),
                          str(self._d(INFLIGHT) / fname))
            except OSError as e:
                if e.errno == errno.ENOENT:
                    return None
                raise OutboxConfigError(f"claim {item_id!r}: {e}") from e
            return ClaimToken(item_id=item_id, worker=worker,
                              incarnation=fname)

    def complete(self, token: ClaimToken, outcome: DeliveryOutcome,
                 park_at_attempts: Optional[int] = None,
                 provider: Optional[str] = None,
                 destination: Optional[str] = None) -> bool:
        parts = token.incarnation.split(SEP)
        if len(parts) != TOKEN_PARTS or parts[1] != _safe_component(token.worker):
            return False                    # forged: worker != the record's
        key = parts[0]
        src = self.root / INFLIGHT / token.incarnation
        with self._lock(key):
            # The filename IS the record: presence + worker match above are
            # the whole ownership check.
            if not src.exists():
                return False
            if outcome is DeliveryOutcome.CONFIRMED:
                dst = self._d(ARCHIVE) / f"{token.incarnation}{SEP}{time.time_ns()}"
                os.rename(str(src), str(dst))
                self._attempts_path(key).unlink(missing_ok=True)
                return True
            if outcome is DeliveryOutcome.OUTCOME_UNKNOWN:
                self._quarantine(src, key, "outcome-unknown", str(time.time_ns()))
                return True
            n = self._note_attempt(key)
            if park_at_attempts is not None and n >= park_at_attempts:
                self._quarantine(src, key, "max-attempts", str(time.time_ns()))
                return True
            # retryable: back to the single ready slot; a re-publish racing us
            # into that slot quarantines this copy (duplicate precursor).
            if not _move(src, self._d(READY) / key) and src.exists():
                self._quarantine(src, key, "collision", str(time.time_ns()))
            return True

    def _attempts_path(self, key: str) -> Path:
        return self._d(ATTEMPTS) / key

    def _note_attempt(self, key: str) -> int:
        p = self._attempts_path(key)
        n = self.attempts_by_key(key) + 1
        tmp = p.with_name(p.name + f".{os.getpid()}.{time.time_ns()}")
        tmp.write_text(str(n), encoding="utf-8")
        os.replace(str(tmp), str(p))
        return n

    def is_terminal(self, item_id: str) -> bool:
        # C records terminality by LOCATION, not a status field: ARCHIVE and
        # PARKED entries lead with the item key as their first SEP component.
        key = _safe_key(item_id)
        prefix = key + SEP
        for name in (ARCHIVE, PARKED):
            d = self._d(name)
            try:
                if any(e.name.startswith(prefix) for e in d.iterdir()):
                    return True
            except FileNotFoundError:
                continue
        return False

    def attempts_by_key(self, key: str) -> int:
        try:
            return int(self._attempts_path(key).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return 0

    def attempts(self, item_id: str) -> int:
        return self.attempts_by_key(_safe_key(item_id))

    def park(self, item_id: str, reason: str) -> None:
        key = _safe_key(item_id)
        with self._lock(key):
            ready = self.root / READY / key
            if ready.exists():
                self._quarantine(ready, key, _safe_component(reason),
                                 str(time.time_ns()))
                return
            for f in self._tokens(key):
                self._quarantine(f, key, _safe_component(reason),
                                 str(time.time_ns()))

    def recover(self) -> RecoverReport:
        """Dead OWNERS' items return to ready/; UNKNOWN never touched. The
        owner is the INCARNATION, not the pid: an ALIVE pid whose birth
        mismatches the token is a reused pid — the claimant is dead."""
        rep = RecoverReport()
        for f in sorted(self._d(INFLIGHT).iterdir()):
            parts = f.name.split(SEP)
            if len(parts) != TOKEN_PARTS or not parts[2].isdigit():
                continue
            key = parts[0]
            ident = outbox.process_identity(int(parts[2]))
            if ident.state is outbox.OwnerState.UNKNOWN:
                continue
            if ident.state is not outbox.OwnerState.DEAD and (
                    ident.start_usec is None
                    or str(ident.start_usec) == parts[3]):
                continue                    # genuinely the live holder
            with self._lock(key):
                if not f.exists():
                    continue
                live, _dead = self._live_and_dead(key)
                if any(t.name != f.name for t in live):
                    self._quarantine(f, key, "live-holder", parts[2])
                    rep.quarantined.append(key)
                    continue
                if _move(f, self._d(READY) / key):
                    rep.recovered.append(key)
                elif f.exists():            # ready/ occupied by a re-publish
                    self._quarantine(f, key, "collision", parts[2])
                    rep.quarantined.append(key)
        return rep

    def cleanup(self, max_age_s: float = 7 * 86400.0,
                now: Optional[float] = None) -> CleanupReport:
        now = time.time() if now is None else now
        pruned = 0
        for d in (ARCHIVE, PARKED, TMP, ATTEMPTS):
            for f in list(self._d(d).iterdir()):
                try:
                    if now - f.stat().st_mtime <= max_age_s:
                        continue
                    if d == ATTEMPTS:
                        # A LIVE item's counter is its park ceiling; pruning
                        # it by age alone lets slow failure evade the cap.
                        key = f.name
                        if ((self.root / READY / key).exists()
                                or self._tokens(key)):
                            continue
                    f.unlink(missing_ok=True)
                    pruned += 1
                except FileNotFoundError:
                    pass
                except OSError as e:
                    raise OutboxConfigError(f"cleanup {f}: {e}") from e
        return CleanupReport(pruned=pruned, detail="C: namespace sweep")

    def force_release(self, item_id: str) -> bool:
        """Administrative requeue: whatever holds the item at the lock
        instant loses it; True = nobody holds it afterwards."""
        key = _safe_key(item_id)
        with self._lock(key):
            live, dead = self._live_and_dead(key)
            for f in (*live, *dead):
                if not _move(f, self._d(READY) / key) and f.exists():
                    self._quarantine(f, key, "forced", str(time.time_ns()))
            return True
