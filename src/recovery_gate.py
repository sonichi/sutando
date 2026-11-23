#!/usr/bin/env python3
"""The one freshness and single-use gate for every way an owner's recovery command reaches the host.

Every entry path calls `admit` before anything runs: the live monitor's bare reply, the launcher's
startup sweep of the same replies, and a recovery card's tap. A command is admitted only while it
is fresh (issued within TTL_S, and not from the future) and only once: admission is a claim file
created exclusively and fsync'd, with its directory, before the action runs. A claim still
`accepted` when its path comes back (a crash between admission and `finish`) is reported as
interrupted and never run again.

Claims: state/recovery/claims/<digest of the key>.json
  {"key", "issued_at", "accepted_at", "state": "accepted"|"finished", "outcome"?}
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import status_path  # noqa: E402

#: How long an owner's command stays actionable after it was issued.
TTL_S = 10 * 60
#: A command stamped further ahead than this is not trusted to be fresh.
FUTURE_SKEW_S = 60
#: Finished claims older than this are dropped; a command that old is stale anyway.
KEEP_S = 7 * 24 * 3600

ACCEPTED, FINISHED = "accepted", "finished"


class Admission(NamedTuple):
    ok: bool
    why: Optional[str]  # None when admitted; "stale", "replay" or "interrupted" otherwise


def claims_dir(workspace: Path) -> Path:
    return status_path("recovery", Path(workspace)) / "claims"


def _path(workspace: Path, key: str) -> Path:
    return claims_dir(workspace) / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json")


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fresh(issued_at, now: float, ttl: float = TTL_S) -> bool:
    if isinstance(issued_at, bool) or not isinstance(issued_at, (int, float)) or not math.isfinite(issued_at):
        return False
    return -FUTURE_SKEW_S <= now - issued_at <= ttl


def state(workspace: Path, key: str) -> Optional[dict]:
    try:
        data = json.loads(_path(workspace, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def admit(workspace: Path, key: str, issued_at, now: Optional[float] = None, ttl: float = TTL_S) -> Admission:
    """Admit `key` once, while fresh. On True the claim is on disk before the caller acts."""
    at = time.time() if now is None else now
    prior = state(workspace, key)
    if prior is not None:
        return Admission(False, "interrupted" if prior.get("state") == ACCEPTED else "replay")
    if not fresh(issued_at, at, ttl):
        return Admission(False, "stale")
    d = claims_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"key": key, "issued_at": issued_at, "accepted_at": at, "state": ACCEPTED}).encode()
    try:
        fd = os.open(str(_path(workspace, key)), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return Admission(False, "replay")
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(d)
    _prune(d, at)
    return Admission(True, None)


def finish(workspace: Path, key: str, outcome: str, now: Optional[float] = None) -> None:
    """Record that an admitted command ran to its end (whatever its outcome)."""
    prior = state(workspace, key) or {"key": key}
    path = _path(workspace, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".claim-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dict(prior, state=FINISHED, outcome=outcome,
                       finished_at=time.time() if now is None else now), f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _prune(d: Path, now: float) -> None:
    for p in d.glob("*.json"):
        try:
            if now - p.stat().st_mtime > KEEP_S and json.loads(p.read_text()).get("state") == FINISHED:
                p.unlink()
        except (OSError, ValueError, AttributeError):
            continue
