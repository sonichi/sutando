"""The detached worker a card's switch starts: its pid on record, and whether it has died before finishing.

A card answers "a switch is already under way" from its record of the attempt; once that attempt's
worker is proven gone (its pid ended, or now names another process) the record is a leftover. The
worker's pid and start time sit beside the card's record, in `<record stem>.worker.json`. A worker whose
start time could not be read is not recorded, so it is never proven dead. Stdlib only.
"""
from __future__ import annotations

import json
from pathlib import Path

import holder_lock
from local_record import write_whole


def path(record: Path) -> Path:
    return record.with_name(record.stem + ".worker.json")


def note(record: Path, attempt: str, pid: int) -> None:
    """Record `pid` as the worker of `attempt`; nothing when its start time cannot be read or kept.
    Best-effort: the worker is already running, so a failed write must not read as a failed launch."""
    try:
        start = holder_lock.start_time(pid)
        if start:
            write_whole(path(record), {"attempt": attempt, "pid": int(pid), "start": start})
    except Exception:
        pass


def dead(record: Path, attempt: str) -> bool:
    """True only when the worker recorded for `attempt` is proven no longer running."""
    try:
        w = json.loads(path(record).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(w, dict) or w.get("attempt") != attempt or not isinstance(w.get("pid"), int):
        return False
    return holder_lock.verify_lineage(w["pid"], str(w.get("start") or "")) is False
