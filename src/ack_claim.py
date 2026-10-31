"""First-writer-wins claims on an acknowledgement key, shared by every sender of that ack.

One record per key under <workspace>/state/ack-claims/. `claim()` creates it only where none
exists, so of two senders racing for `ack:<task>:initial` exactly one gets True and speaks; the
other stays silent. Only the holder may change or release its record, and it releases only
when nothing was sent. A record that exists but cannot be read counts as held: two lines are
worse than none. States: claimed (sending, outcome not yet known), confirmed (delivery
evidence recorded), unknown (the send may have landed). Each claim carries a `gen`; a caller that
passes it back to update/release changes only that claim, never a successor created after a release
under the same holder name. `prune()` drops only records its caller no longer keeps.
Contract: tests/ack-claim.test.py.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_record  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

STATES = ("claimed", "confirmed", "unknown")
_KEY = re.compile(r"\A[a-z][a-z-]*:[A-Za-z0-9._-]{1,160}:[a-z][a-z-]*\Z")


def claims_dir(workspace: Optional[Path] = None) -> Path:
    return Path(workspace or resolve_workspace(migrate=False)) / "state" / "ack-claims"


def _name(key: str) -> str:
    if not isinstance(key, str) or not _KEY.match(key):
        raise ValueError(f"not an ack key: {key!r}")
    return key.replace(":", "+")


def _path(workspace: Path, key: str) -> Path:
    return claims_dir(workspace) / f"{_name(key)}.json"


def claim(workspace: Path, key: str, holder: str, now: Optional[float] = None, **fields):
    """The new claim's generation (a truthy string) when this call created it and may send; False
    when it already exists."""
    gen = secrets.token_hex(8)
    rec = {**fields, "id": _name(key), "key": key, "holder": holder, "state": "claimed",
           "claimed_at": time.time() if now is None else now, "gen": gen}
    try:
        local_record.create_text_whole(_path(workspace, key), json.dumps(rec, ensure_ascii=False))
    except FileExistsError:
        return False
    return gen


#: Never unlinked: shared around opening and acquiring a key lock, exclusive while removing one.
DIR_LOCK = ".dir.lock"
#: A test seam called after a key lock is opened and before it is acquired.
_after_open = None


@contextlib.contextmanager
def _dir_lock(d: Path, mode: int):
    with open(d / DIR_LOCK, "a") as fh:
        fcntl.flock(fh, mode)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextlib.contextmanager
def _locked(workspace: Path, key: str):
    """Serialises the read-check-write of update/release on one key across processes. The key
    lock is opened and acquired under the shared directory lock, so cleanup never unlinks it
    in between; the path must still name the acquired inode, else it is opened again."""
    d = claims_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_name(key)}.lock"
    while True:
        with _dir_lock(d, fcntl.LOCK_SH):
            fh = open(path, "a")
            if _after_open is not None:
                _after_open(key)
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            same = os.fstat(fh.fileno()).st_ino == os.stat(path).st_ino
        except FileNotFoundError:
            same = False
        if same:
            break
        fh.close()
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _mine(d, key: str, holder: str, gen) -> bool:
    return (d is not None and d.get("holder") == holder and d.get("id") == _name(key)
            and (gen is None or d.get("gen") == gen))


def read(workspace: Path, key: str) -> Optional[dict]:
    """The record, None when there is none, or {"state": "unknown"} when one exists but is unreadable."""
    p = _path(workspace, key)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"key": key, "state": "unknown", "holder": None}
    if not isinstance(d, dict) or d.get("key") != key or d.get("state") not in STATES:
        return {"key": key, "state": "unknown", "holder": d.get("holder") if isinstance(d, dict) else None}
    return d


def state(workspace: Path, key: str) -> str:
    """"none" when unclaimed, else the record's state; an unreadable record reads "unknown"."""
    d = read(workspace, key)
    return "none" if d is None else d["state"]


def update(workspace: Path, key: str, holder: str, new_state: Optional[str] = None,
           gen: Optional[str] = None, **fields) -> bool:
    """Merge `fields` (and a new state) into the holder's own record; False when it is not the
    holder's, or not the generation `gen` names."""
    if new_state is not None and new_state not in STATES:
        raise ValueError(f"not an ack-claim state: {new_state!r}")
    with _locked(workspace, key):
        d = read(workspace, key)
        if not _mine(d, key, holder, gen):
            return False
        d.update(fields)
        if new_state is not None:
            d["state"] = new_state
        local_record.write_text_whole(_path(workspace, key), json.dumps(d, ensure_ascii=False))
    return True


def release(workspace: Path, key: str, holder: str, gen: Optional[str] = None) -> bool:
    """Drop the holder's own claim, for a sender that sent nothing; False when it is not the
    holder's, or not the generation `gen` names."""
    with _locked(workspace, key):
        d = read(workspace, key)
        if not _mine(d, key, holder, gen):
            return False
        _path(workspace, key).unlink(missing_ok=True)
    return True


def held_by(workspace: Path, holder: str, state_filter: Optional[str] = None) -> list:
    """Every readable record this holder owns, optionally only in one state."""
    d = claims_dir(workspace)
    out = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(rec, dict) and rec.get("holder") == holder and rec.get("id") == p.stem
                and (state_filter is None or rec.get("state") == state_filter)):
            out.append(rec)
    return out


def prune(workspace: Path, keep, now: float, retention_s: float) -> list:
    """Remove each record past `retention_s` that `keep(record)` does not keep, then the orphaned
    key locks. An unreadable record is never removed; the caller's `keep` decides what is live."""
    d = claims_dir(workspace)
    removed = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = rec.get("key") if isinstance(rec, dict) else None
        if not isinstance(key, str) or not _KEY.match(key) or rec.get("id") != p.stem:
            continue
        age = now - rec["claimed_at"] if isinstance(rec.get("claimed_at"), (int, float)) else None
        if age is None or age < retention_s or keep(rec):
            continue
        with _locked(workspace, key):
            p.unlink(missing_ok=True)
        removed.append(key)
    return removed + prune_orphan_locks(workspace, now, retention_s)


def prune_orphan_locks(workspace: Path, now: float, retention_s: float) -> list:
    """Remove each key lock whose record is gone, untouched for `retention_s`, and that nobody
    holds: under the exclusive directory lock, so no publisher is between opening and acquiring it."""
    d = claims_dir(workspace)
    if not d.is_dir():
        return []
    removed = []
    with _dir_lock(d, fcntl.LOCK_EX):
        for lock in sorted(d.glob("*.lock")):
            if lock.name == DIR_LOCK or (d / f"{lock.stem}.json").exists():
                continue
            try:
                if now - lock.stat().st_mtime < retention_s:
                    continue
                fh = open(lock, "a")
            except OSError:
                continue
            with fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue
                if os.fstat(fh.fileno()).st_ino == os.stat(lock).st_ino:
                    lock.unlink()
                    removed.append(f"lock:{lock.stem}")
    return removed
