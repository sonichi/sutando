"""The two records the presence daemon reconciles, and how they are written.

Each record has exactly one writer contract:

  desired.json — written by agents through `mutate_desired`, which takes an
                 exclusive lock: several agents on one host read a summon at
                 the same time, and a read-modify-write without the lock loses
                 whichever registration landed second.

  live.json    — written only by the daemon, so it needs no lock. It is still
                 published atomically: a reader that catches a half-written
                 file would read a shorter connection list as "these surfaces
                 were dropped".

Both publish through a temp file plus `os.replace`, never by truncating the
target in place — a reader polling during a truncate sees a zero-length file,
which parses as "nothing is connected".
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

SCHEMA = 1


def _publish(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class RecordUnreadable(OSError):
    """The record exists but could not be read. Distinct from absent, because
    the two mean opposite things about what the user wants."""


def read_entries(path: Path) -> list[dict]:
    """The record's entries, or none.

    Absent, empty, truncated or of another schema reads as empty: there is
    nothing to act on. A record that EXISTS and cannot be read raises instead
    — read as empty it says "the user wants nothing", and the daemon would
    evict the agent from every surface over a transient permission blip.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except ValueError:
        return []
    except OSError as exc:
        raise RecordUnreadable(f"{path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("v") != SCHEMA:
        return []
    entries = data.get("entries")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def write_entries(path: Path, entries: list[dict]) -> None:
    _publish(path, {"v": SCHEMA, "entries": list(entries)})


@contextmanager
def _exclusive(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    with open(lock, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def mutate_desired(path: Path, change) -> list[dict]:
    """Apply `change(entries) -> entries` under an exclusive lock, and publish.

    The whole read-modify-write is inside the lock. Locking only the write
    would still lose an entry: two agents would each read the old list and the
    later publish would overwrite the earlier one's addition.
    """
    with _exclusive(path):
        entries = change(read_entries(path))
        write_entries(path, entries)
        return entries


def upsert(entries: list[dict], entry: dict) -> list[dict]:
    """Replace the entry for this surface, or add it. A second summon into a
    surface already registered refreshes `summoned_at` — which is what brings
    a surface back after it idled out — rather than duplicating it."""
    key = (entry.get("room"), entry.get("kind"))
    kept = [e for e in entries if (e.get("room"), e.get("kind")) != key]
    return kept + [entry]


def without(entries: list[dict], room: str, kind: str) -> list[dict]:
    return [e for e in entries if (e.get("room"), e.get("kind")) != (room, kind)]
