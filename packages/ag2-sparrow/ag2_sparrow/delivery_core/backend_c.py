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
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Optional

from .. import outbox
from .contract import (BackendCapabilities, ClaimToken, CleanupReport,
                       DeliveryOutcome, RecoverReport)

TMP, READY, INFLIGHT, ARCHIVE, PARKED, ATTEMPTS = (
    "tmp", "ready", "inflight", "archive", "undelivered", "attempts")
# tmp-stage terminal records carry this tag so publish's tmp files never match.
TERMINAL_TAG = "terminal"
SEP = "~"
TOKEN_PARTS = 5


def is_producer_token(name: str) -> bool:
    """A name C's claim writer could have produced, arity AND pid.

    recover() and _live_and_dead() both ignore a non-numeric pid forever, so a
    caller that checks only arity calls a permanently-unrecoverable name valid.
    """
    parts = name.split(SEP)
    # isascii() too: isdigit alone admits '\u00b2' (int() raises in recover)
    # and '\u0663' (int() accepts it; str(os.getpid()) cannot emit it).
    return (len(parts) == TOKEN_PARTS
            and parts[2].isascii() and parts[2].isdigit())

_ACTIVATED: set[str] = set()
_ACTIVATE_GUARD = threading.Lock()


class OutboxConfigError(RuntimeError):
    """Non-race filesystem failure (permissions, cross-device, ...): the
    root is misconfigured. Raised, never folded into a lost-race outcome."""


class InvariantError(RuntimeError):
    """Two simultaneous LIVE holders for one key — a state with no producer.
    Raw-token plurality is NOT this error (dead ghosts are anticipated)."""


def _record_is_terminal_proof(rec) -> bool:
    """The ONE total validator — shared by staged promotion, archive
    retirement, and reads. Divergent copies are the failure it prevents."""
    if not isinstance(rec, dict) or not _exact_int(rec.get("schema")) \
            or rec.get("schema") != 1:
        return False
    item_id = rec.get("item_id")
    # "" is a legal id (publish("") is contract-valid); the _safe_key
    # binding, not truthiness, is what discriminates.
    if not isinstance(item_id, str):
        return False
    if rec.get("outcome") != DeliveryOutcome.CONFIRMED.value:
        return False                     # C stages terminals ONLY for confirmed
    receipt = rec.get("receipt")
    # _write_terminal always emits both keys (values may be None); a
    # receipt without them was not produced by the writer.
    if not isinstance(receipt, dict) \
            or "provider" not in receipt or "destination" not in receipt:
        return False
    if not _exact_int(rec.get("completed_ns")) \
            or not _exact_int(rec.get("attempts")):
        return False
    worker = rec.get("worker")
    # "" stays rejected here: _safe_component refuses it at claim time,
    # so no real incarnation can carry an empty worker.
    if not isinstance(worker, str) or not worker:
        return False
    inc = rec.get("incarnation")
    if not isinstance(inc, str):
        return False
    iparts = inc.split(SEP)
    if not iparts or iparts[0] != _safe_key(item_id):
        return False                     # record's own incarnation/id split
    if len(iparts) >= 2 and _safe_component(worker) != iparts[1]:
        return False
    # Arity is policy: native claims are EXACTLY five parts (producer
    # grammar), imports EXACTLY two. No writer emits anything else.
    if len(iparts) == 5:
        if not is_producer_token(inc):
            return False
    elif len(iparts) == 2:
        # The importer is the ONLY 2-part writer: require its full
        # provenance, or arbitrary on-disk JSON becomes delivery proof.
        if rec.get("imported") is not True or worker != "a-import":
            return False
        dig = rec.get("a_record_digest")
        if not isinstance(dig, str) or len(dig) != 64 \
                or any(ch not in "0123456789abcdef" for ch in dig):
            return False
    else:
        return False
    return True


def read_terminal_records(root: Path, item_id: str) -> "list[dict]":
    """Pure read of a root's terminal records — no dir creation, no fence
    check, usable on a root no backend has been constructed for (audit and
    migration-window resolvers). The backend method delegates here."""
    key = _safe_key(item_id)
    out = []
    arch = Path(root) / ARCHIVE
    if not arch.is_dir():
        return out
    for f in arch.glob(f"{key}*.json"):
        if f.name != f"{key}.json" and not f.name.startswith(f"{key}{SEP}"):
            continue                      # a longer key sharing the prefix
        data = _regular_json(f)
        if data is None:
            continue
        if _record_is_terminal_proof(data) \
                and data.get("item_id") == item_id:
            out.append(data)
    # cycle FIRST: a clock correction must not let an older receipt win.
    # completed_ns only breaks ties (legacy records carry no cycle -> 0).
    out.sort(key=lambda r: (r.get("cycle") if _exact_int(r.get("cycle")) else 0,
                            r["completed_ns"]))
    return out


def _safe_key(item_id: str) -> str:
    stub = "".join(c if (c.isalnum() or c in "-._") else "_" for c in item_id)[:60]
    return f"{stub}={hashlib.sha256(item_id.encode('utf-8')).hexdigest()[:16]}"


def _safe_component(s: str) -> str:
    out = "".join(c if (c.isalnum() or c in "-._") else "_" for c in s)[:40]
    if not out:
        raise ValueError("empty component")
    return out


def _exact_int(v) -> bool:
    """bool subclasses int, so `isinstance(v, int)` admits True/False and
    `True == 1` passes an equality gate — a record the writer cannot emit."""
    return isinstance(v, int) and not isinstance(v, bool)


def _regular_json(f: Path):
    """Parsed JSON from a REGULAR, non-symlink file, else None.

    A symlink named like a valid entry is promotable proof whose bytes live
    outside the store: removing the target deletes the only record.
    """
    try:
        st = os.lstat(f)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


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

    # Terminal records are JSON (archive/<key>.json), so receipts persist.
    persists_receipt_metadata = True

    capabilities = BackendCapabilities(supports_force_release=True)

    def __init__(self, root: Path, activate: bool = False,
                 durability: str = "default"):
        # "default": fsync the terminal record before its archive rename;
        # "strict": also fsync the archive dir; "lax": no fsync (tests/bench).
        self.durability = durability
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
            if not is_producer_token(f.name):
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
        # item_id must bind to the incarnation's key BEFORE any mutation: an
        # inconsistent token would archive an unfindable receipt.
        if _safe_key(token.item_id) != key:
            return False
        src = self.root / INFLIGHT / token.incarnation
        with self._lock(key):
            # The filename IS the record: presence + worker match above are
            # the whole ownership check.
            if not src.exists():
                return False
            if outcome is DeliveryOutcome.CONFIRMED:
                record = {
                    "schema": 1, "item_id": token.item_id,
                    "outcome": outcome.value,
                    "receipt": {"provider": provider, "destination": destination},
                    "completed_ns": time.time_ns(),
                    "worker": token.worker, "attempts": self.attempts(token.item_id),
                    "incarnation": token.incarnation,
                }
                self._write_terminal(key, record, token.incarnation)
                src.unlink(missing_ok=True)          # D: release the claim
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

    def _terminal_path(self, key: str) -> Path:
        return self._d(ARCHIVE) / f"{key}.json"

    def _next_cycle(self, key: str) -> int:
        """Durable logical cycle for `key`: one past the highest already recorded.

        The archive (plus any staged record not yet finalized) IS the ledger, so
        ordering never depends on `time.time_ns()` moving forward. Callers hold
        the per-key lock, which is what makes read-max-then-write safe.
        """
        hi = 0
        for f in self._d(ARCHIVE).glob(f"{key}*.json"):
            rec = _regular_json(f)
            if isinstance(rec, dict) and _exact_int(rec.get("cycle")):
                hi = max(hi, rec["cycle"])
        for f in self._d(TMP).glob(f"{TERMINAL_TAG}{SEP}{key}{SEP}*.json"):
            rec = _regular_json(f)
            if isinstance(rec, dict) and _exact_int(rec.get("cycle")):
                hi = max(hi, rec["cycle"])
        return hi + 1

    def _write_terminal(self, key: str, record: dict, incarnation: str) -> None:
        """R->F->M of the terminal protocol: atomic tmp write, fsync per the
        durability mode, then rename into archive/. Caller releases the claim.
        The staging name embeds the INCARNATION so recovery can bind a staged
        record to exactly the claim that produced it, never a sibling's."""
        record = dict(record, cycle=self._next_cycle(key))
        tmp = self._d(TMP) / f"{TERMINAL_TAG}{SEP}{incarnation}{SEP}{time.time_ns()}.json"
        data = json.dumps(record).encode()
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            off = 0
            while off < len(data):
                # os.write may legally write fewer bytes; a truncated record
                # finalizing as the item's only receipt is the hole C closes.
                off += os.write(fd, data[off:])
            if self.durability != "lax":
                os.fsync(fd)
        finally:
            os.close(fd)
        dst = self._terminal_path(key)
        if dst.exists():
            # Same id redelivered after a prior terminal: keep both records.
            dst = dst.with_name(f"{key}{SEP}{time.time_ns()}.json")
        os.rename(str(tmp), str(dst))
        self._strict_dir_barrier()

    def _strict_dir_barrier(self) -> None:
        """strict mode: the archive DIRECTORY entry is durable before any
        claim release — shared by complete() and recovery finalization."""
        if self.durability != "strict":
            return
        dfd = os.open(str(self._d(ARCHIVE)), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def terminal_record(self, item_id: str) -> "dict | None":
        """The LATEST terminal record for `item_id` (max completed_ns), or
        None. Republished ids accrete records; the current cycle wins here,
        and terminal_records() returns the full history."""
        recs = self.terminal_records(item_id)
        return recs[-1] if recs else None

    def terminal_records(self, item_id: str) -> "list[dict]":
        """All terminal records for `item_id`, oldest first by completed_ns.
        Legacy filename-format entries carry no receipt and are not listed.
        Records failing the total validator are never returned as proof."""
        return read_terminal_records(self.root, item_id)

    _record_is_terminal_proof = staticmethod(_record_is_terminal_proof)

    @classmethod
    def _staged_is_complete(cls, staged, incarnation: str) -> bool:
        """Authoritative = a record _write_terminal() could have produced,
        bound to the staging filename's incarnation. Anything less is torn."""
        return cls._record_is_terminal_proof(staged) \
            and staged.get("incarnation") == incarnation

    def _incarnation_is_terminal(self, key: str, incarnation: str) -> bool:
        """True iff an archive entry records THIS incarnation as completed:
        a COMPLETE JSON record bound to it, or a legacy rename whose filename
        begins with the incarnation. Keyed by claim, never item id."""
        for f in self._d(ARCHIVE).iterdir():
            if f.name.startswith(incarnation):
                # Only the exact legacy grammar (regular file, incarnation+
                # SEP+nanos) is rename-atomic evidence; prefix-shares are not.
                suffix = f.name[len(incarnation):]
                # is_file() follows symlinks; a symlink is never rename-atomic.
                if not f.is_symlink() and f.is_file() and suffix.startswith(SEP)                         and suffix[len(SEP):].isdigit():
                    return True
                continue
            if not (f.name.startswith(f"{key}") and f.suffix == ".json"):
                continue
            rec = _regular_json(f)
            if rec is None:
                continue
            # A record authorizes retiring a LIVE claim only when it passes
            # the same total validation as staging; malformed fails closed.
            if self._staged_is_complete(rec, incarnation):
                return True
        return False

    def recover(self) -> RecoverReport:
        """Dead OWNERS' items return to ready/; OwnerState.UNKNOWN (process
        liveness, NOT DeliveryOutcome.OUTCOME_UNKNOWN) never touched. The
        owner is the INCARNATION, not the pid: an ALIVE pid whose birth
        mismatches the token is a reused pid — the claimant is dead."""
        rep = RecoverReport()
        # R-M window: a PARSEABLE staged record finalizes (outcome decided);
        # a torn temp is deleted — dead-claim recovery redelivers instead.
        for t in sorted(self._d(TMP).glob(f"{TERMINAL_TAG}{SEP}*.json")):
            inc = t.name[len(TERMINAL_TAG) + len(SEP):].rsplit(SEP, 1)[0]
            key = inc.split(SEP)[0]
            with self._lock(key):
                if not t.exists():
                    continue
                # None (torn, OR a symlink whose bytes live outside the store)
                # falls through: _staged_is_complete refuses it, then it is deleted.
                staged = _regular_json(t)
                if not self._staged_is_complete(staged, inc):
                    t.unlink(missing_ok=True)      # torn write, never authoritative
                    continue
                if self.durability != "lax":
                    # Parseable is not durable: the crashed writer may not
                    # have reached its fsync. Re-barrier before finalizing.
                    fd = os.open(str(t), os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                dst = self._terminal_path(key)
                if dst.exists():
                    dst = dst.with_name(f"{key}{SEP}{time.time_ns()}.json")
                os.rename(str(t), str(dst))
                self._strict_dir_barrier()   # durable BEFORE the release
                (self.root / INFLIGHT / inc).unlink(missing_ok=True)
                # Retirement ends the cycle, so its attempt budget dies with it —
                # a republished item must start at 0, not inherit a spent count.
                self._attempts_path(key).unlink(missing_ok=True)
                rep.retired.append(key)
        for f in sorted(self._d(INFLIGHT).iterdir()):
            parts = f.name.split(SEP)
            if not is_producer_token(f.name):
                continue
            key = parts[0]
            # M-D window: retire ONLY a claim whose OWN incarnation is
            # terminal — an older record must not kill a live redelivery.
            if self._incarnation_is_terminal(key, f.name):
                with self._lock(key):
                    if f.exists():
                        # M-D: the archive ENTRY must be durable before the
                        # claim dies, or a crash strands neither proof nor claim.
                        self._strict_dir_barrier()
                        f.unlink(missing_ok=True)
                        self._attempts_path(key).unlink(missing_ok=True)
                        rep.retired.append(key)
                continue
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
                # A crash inside _quarantine leaves the claim's INODE already
                # linked in undelivered/; finish that quarantine, never re-ready.
                def _is_twin(q, dev, ino):
                    # Fail-closed: the real twin is a REGULAR-file hardlink,
                    # same (st_dev, st_ino) — inodes repeat across filesystems.
                    try:
                        st = os.lstat(str(q))
                    except OSError:
                        return False
                    import stat as _stat
                    return (_stat.S_ISREG(st.st_mode)
                            and st.st_dev == dev and st.st_ino == ino)
                try:
                    _cst = os.lstat(str(f))
                    claim_dev, claim_ino = _cst.st_dev, _cst.st_ino
                except OSError:
                    continue
                if any(_is_twin(q, claim_dev, claim_ino)
                       for q in self._d(PARKED).glob(f"{key}{SEP}*")):
                    f.unlink(missing_ok=True)
                    rep.quarantined.append(key)
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
