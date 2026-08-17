#!/usr/bin/env python3
"""Design B prototype v2: the payload's LOCATION is the state; every legal
transition is one atomic namespace operation on the SOURCE.

    tmp/<id>.<nonce>                 being written (never read by anyone)
    ready/<safe_id>                  available
    inflight/<safe_id>~<worker>~<pid>~<birth>   claimed by exactly one worker
    archive/<safe_id>[...]           delivered
    undelivered/<safe_id>[...]       parked (gave up, or collision quarantine)

v2 closes the protocol gaps from the 2026-08-17 owner review:
  - atomic publish: tmp write -> link into ready/ (EEXIST = already published,
    never a silent overwrite) -> unlink tmp. A crash leaves only tmp/ debris,
    swept by cleanup(); ready/ can never hold a torn payload.
  - filename schema: components are sanitized (id via safe_key: readable stub +
    digest; worker restricted) and joined with '~', which no component may
    contain — an id with '.' or '~' in it can no longer corrupt the parse.
  - destination collisions are explicit: claim/complete use plain rename to
    names that cannot pre-exist (token / token-tailed terminal names), keeping
    every transfer a single atomic linearization point; only recover's landing
    on the stable ready/<key> slot needs create-if-absent (link+unlink), and an
    occupied destination quarantines into undelivered/ — never a silent
    overwrite. (v2.0 used link+unlink everywhere; fault injection showed the
    crash window between link and unlink leaves TWO live names for one inode —
    rename's atomicity was the load-bearing property, now restored.)
  - error classification: lost races (ENOENT/EEXIST) are protocol outcomes;
    everything else (EACCES, EXDEV, ...) raises OutboxConfigError — a
    misconfigured root must be loud, not a permanent silent None.
  - cleanup(): retention pruning for archive/ + undelivered/ and tmp-debris
    sweep — bounded on-disk state, the fifth op the state machine drives.
"""
from __future__ import annotations

import errno
import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import outbox as ob  # ALIVE/DEAD/UNKNOWN process oracle  # noqa: E402

TMP, READY, INFLIGHT, ARCHIVE, PARKED = "tmp", "ready", "inflight", "archive", "undelivered"
SEP = "~"


class OutboxConfigError(RuntimeError):
    """Non-race filesystem failure (permissions, cross-device, ...): the root
    is misconfigured. Raised, never folded into a lost-race None/False."""


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


def publish(root, item_id: str, body: str = "") -> bool:
    """True = published. False = an item with this id is already in ready/."""
    key = safe_key(item_id)
    tmp = _d(root, TMP) / f"{key}{SEP}{os.getpid()}{SEP}{time.time_ns()}"
    tmp.write_text(body, encoding="utf-8")
    dst = _d(root, READY) / key
    try:
        os.link(str(tmp), str(dst))            # atomic create-if-absent
    except OSError as e:
        tmp.unlink(missing_ok=True)
        if e.errno == errno.EEXIST:
            return False
        raise OutboxConfigError(f"publish {item_id!r}: {e}") from e
    tmp.unlink(missing_ok=True)
    return True


def claim(root, item_id: str, worker: str):
    """Claim by moving the source; exactly one racer wins. None = lost/absent."""
    key = safe_key(item_id)
    ident = ob.process_identity(os.getpid())
    token = SEP.join((key, _safe_component(worker), str(os.getpid()),
                      str(ident.start_usec)))
    # The token is unique per claimer incarnation, so the destination cannot
    # pre-exist: plain rename is atomic AND the single linearization point —
    # exactly one racer's rename finds the source (losers get ENOENT).
    try:
        os.rename(str(_d(root, READY) / key), str(_d(root, INFLIGHT) / token))
    except OSError as e:
        if e.errno == errno.ENOENT:
            return None
        raise OutboxConfigError(f"claim {item_id!r}: {e}") from e
    return token


def complete(root, token: str, terminal: str = ARCHIVE) -> bool:
    """Finish a claim. Terminal names carry the token's tail, so the
    destination is always fresh: one atomic rename, overwrite impossible,
    and every delivery keeps its own terminal record."""
    key = token.split(SEP, 1)[0]
    dst = _d(root, terminal) / f"{key}{SEP}{token.split(SEP, 1)[1]}"
    try:
        os.rename(str(_d(root, INFLIGHT) / token), str(dst))
    except OSError as e:
        if e.errno == errno.ENOENT:
            return False                        # src gone: not ours to finish
        raise OutboxConfigError(f"complete {token!r}: {e}") from e
    return True


def recover(root):
    """Return DEAD owners' items to ready/. UNKNOWN/ALIVE are never touched."""
    moved = []
    for f in list(_d(root, INFLIGHT).iterdir()):
        parts = f.name.split(SEP)
        if len(parts) != 4:
            continue
        key, _worker, pid_s, birth = parts
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        ident = ob.process_identity(pid)
        if ident.state is not ob.OwnerState.DEAD:
            continue
        if ident.start_usec is not None and str(ident.start_usec) != birth:
            continue
        # ready/<key> must be create-if-absent (a re-publish may occupy it),
        # so this one transition is link+unlink. Its crash window (linked, not
        # yet unlinked) resolves on the NEXT recover pass: the dead token hits
        # EEXIST against its own earlier link and is quarantined — identical
        # content, never deliverable twice.
        if _move(f, _d(root, READY) / key):
            moved.append(key)
        elif f.exists():                        # ready/ occupied
            _quarantine(f, root, key, "collision", pid_s)
    return moved


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
    key = safe_key(item_id)
    for f in _d(root, INFLIGHT).iterdir():
        parts = f.name.split(SEP)
        if len(parts) == 4 and parts[0] == key:
            return parts[1]
    return None
