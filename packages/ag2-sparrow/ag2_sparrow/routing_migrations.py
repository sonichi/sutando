"""One-time migrations of the owner-routing state, apart from its live writer.

Run before the bridge reads the record. Idempotent: a migrated state is left as it is. Never raises:
the result says whether it finished, so the caller retries an unfinished run at its next load.
Two transformations, both of state from before the record held a binding instead of the gateway URL:
- the record itself, rewritten once in the bound form through the caller's record builder and writer;
- the old writer's crash leftovers, `.<record name>.<pid>.tmp`, which may hold the full URL.
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

TEMP_MAX_AGE_S = 3600.0  # a legacy temp older than this is stale, whatever pid its name carries

DONE, NOT_NEEDED, FAILED = "done", "not_needed", "failed"


@dataclass(frozen=True)
class Result:
    """state DONE: migrated and verified on disk; NOT_NEEDED: nothing to migrate; FAILED:
    unknown (a read error, an unbindable or unwritten record, an unfinished sweep), so the
    caller runs it again. `rewritten`: the record was rewritten and read back, so a reading
    the caller holds is stale and must be reloaded from disk."""
    state: str
    rewritten: bool = False
    removed: int = 0

    @property
    def finished(self) -> bool:
        return self.state != FAILED


def migrate_owner_routing(record_path: Path, *,
                          record_for: Callable[[str, str, str], Optional[dict]],
                          write: Callable[[Path, str], bool],
                          log: Callable[[str], None] = lambda _msg: None) -> Result:
    """Sweep the legacy temps, then rewrite a legacy record. `record_for(identity, full URL,
    owner_dm)` builds the current record or None when it cannot be bound; `write` publishes it
    atomically and says whether it committed. A sweep error never stops the rewrite."""
    path = Path(record_path)
    removed, swept = 0, False
    try:
        removed, swept = sweep_legacy_temps(path)
    except Exception as e:  # noqa: BLE001 - the sweep is retried; the rewrite still runs
        log(f"owner-routing temp sweep failed ({type(e).__name__}) — retried at the next load")
    if removed:
        log(f"owner-routing: removed {removed} stale temp file(s)")
    try:
        state = _rewrite_legacy_record(path, record_for, write)
    except Exception as e:  # noqa: BLE001 - a failed migration is retried, never fatal
        log(f"owner-routing migration failed ({type(e).__name__}) — retried at the next load")
        state = FAILED
    if state == FAILED:
        log("owner-routing: legacy record not migrated yet — retried at the next load")
    rewritten = state == DONE
    if not swept:
        state = FAILED  # the sweep reruns; a verified rewrite still stands
    elif removed and state == NOT_NEEDED:
        state = DONE
    return Result(state=state, rewritten=rewritten, removed=removed)


def _read(path: Path):
    """The record, None when there is none; raises OSError/ValueError when it cannot be read."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _rewrite_legacy_record(path: Path, record_for, write) -> str:
    try:
        d = _read(path)
    except (OSError, ValueError):
        return FAILED  # unknown: it may still hold the URL
    gateway = d.get("gateway") if isinstance(d, dict) else None
    if not isinstance(gateway, str) or not gateway or d.get("binding"):
        return NOT_NEEDED
    record = record_for(str(d.get("identity") or ""), gateway, str(d.get("owner_dm") or ""))
    if record is None or not write(path, json.dumps(record)):
        return FAILED
    try:
        return DONE if _read(path) == record else FAILED  # verified on disk
    except (OSError, ValueError):
        return FAILED


def sweep_legacy_temps(record_path: Path) -> "tuple[int, bool]":
    """Unlink `.<record name>.<pid>.tmp` in the record's dir older than TEMP_MAX_AGE_S: regular
    files only (no symlink is followed), this uid's; the age alone decides, never the pid,
    which may be reused (this process's own included). (removed, finished): an entry that
    errors is skipped, and the sweep is unfinished."""
    pattern = re.compile(re.escape(f".{record_path.name}.") + r"[0-9]{1,10}\.tmp")
    try:
        dfd = os.open(str(record_path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except FileNotFoundError:
        return 0, True
    removed, finished = 0, True
    try:
        for name in os.listdir(dfd):
            if not pattern.fullmatch(name):
                continue
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                        or time.time() - st.st_mtime < TEMP_MAX_AGE_S):
                    continue
                os.unlink(name, dir_fd=dfd)
                removed += 1
            except FileNotFoundError:
                continue  # gone already: nothing left to sweep
            except OSError:
                finished = False
    finally:
        os.close(dfd)
    return removed, finished
