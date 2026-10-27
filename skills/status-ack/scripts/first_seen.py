"""The status-ack runner's durable first-seen record: when it first observed each queued task.

One record per task under <workspace>/state/status-ack/first-seen/, written only here. The
first write is create-only, so a restart or a second pass reads the original stamp and never a
later one. A record that exists but cannot be read is reported as unreadable (the caller then
fails closed). `prune()` bounds the store: it drops a record only once its task has left the
queue, so a queued task's record is never lost. Contract: tests/status-ack-first-seen.test.py.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Callable, Optional

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))
import local_record  # noqa: E402

UNREADABLE = "unreadable"
#: A record whose task left the queue is kept this long, then pruned.
RETENTION_S = 7 * 86400.0
#: Hard cap: past it, the oldest records of tasks no longer queued go first.
MAX_RECORDS = 5000
_TID = re.compile(r"\Atask-[A-Za-z0-9._-]{1,160}\Z")


def store_dir(ws: Path) -> Path:
    return Path(ws) / "state" / "status-ack" / "first-seen"


def _valid(tid) -> bool:
    return isinstance(tid, str) and bool(_TID.match(tid)) and ".." not in tid


def _path(ws: Path, tid: str) -> Path:
    if not _valid(tid):
        raise ValueError(f"not a task id: {tid!r}")
    return store_dir(ws) / f"{tid}.json"


def read(ws: Path, tid: str):
    """The persisted first-seen epoch, None when there is no record, or UNREADABLE."""
    path = _path(ws, tid)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return UNREADABLE
    seen = d.get("first_seen") if isinstance(d, dict) and d.get("task_id") == tid else None
    return float(seen) if isinstance(seen, (int, float)) and not isinstance(seen, bool) else UNREADABLE


def record(ws: Path, tid: str, now: float):
    """The task's first-seen epoch: the existing one, or `now` once this call created it."""
    existing = read(ws, tid)
    if existing is not None:
        return existing
    try:
        local_record.create_text_whole(_path(ws, tid), json.dumps({"task_id": tid, "first_seen": now}))
    except FileExistsError:
        return read(ws, tid)
    return now


def prune(ws: Path, queued: Callable[[str], bool], now: float,
          retention_s: float = RETENTION_S, max_records: int = MAX_RECORDS) -> list:
    """Remove records whose task is no longer queued and that are past retention; past the cap,
    the oldest such records too. A queued task's record, or an unreadable one, is never removed."""
    d = store_dir(ws)
    rows = []
    for p in sorted(d.glob("task-*.json")) if d.is_dir() else []:
        seen = read(ws, p.stem) if _valid(p.stem) else UNREADABLE
        if seen == UNREADABLE or seen is None or queued(p.stem):
            continue
        rows.append((seen, p))
    total = sum(1 for _ in d.glob("task-*.json")) if d.is_dir() else 0
    rows.sort()
    removed = []
    for seen, p in rows:
        if now - seen >= retention_s or total - len(removed) > max_records:
            p.unlink(missing_ok=True)
            removed.append(p.stem)
    return removed
