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

TEMP_GRACE_S = 5.0
TEMP_MAX_AGE_S = 3600.0  # past this a temp is stale whatever its pid says: the pid may be reused


@dataclass(frozen=True)
class Result:
    done: bool
    rewritten: bool = False
    removed: int = 0


def migrate_owner_routing(record_path: Path, *,
                          record_for: Callable[[str, str, str], Optional[dict]],
                          write: Callable[[Path, str], bool],
                          pid_alive: Callable[[int], bool],
                          log: Callable[[str], None] = lambda _msg: None) -> Result:
    """Sweep the legacy temps, then rewrite a legacy record. `record_for(identity, full URL,
    owner_dm)` builds the current record or None when it cannot be bound; `write` publishes it
    atomically and says whether it committed."""
    try:
        removed = sweep_legacy_temps(Path(record_path), pid_alive)
        rewritten = _rewrite_legacy_record(Path(record_path), record_for, write)
    except Exception as e:  # noqa: BLE001 - a failed migration is retried, never fatal
        log(f"owner-routing migration failed ({type(e).__name__}) — retried at the next load")
        return Result(done=False)
    if rewritten is None:
        log("owner-routing: legacy record not migrated yet — retried at the next load")
        return Result(done=False, removed=removed)
    if removed:
        log(f"owner-routing: removed {removed} stale temp file(s)")
    return Result(done=True, rewritten=rewritten, removed=removed)


def _rewrite_legacy_record(path: Path, record_for, write) -> "bool | None":
    """True rewritten, False nothing to do, None not done yet."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return False  # unreadable: the live reader refuses it, nothing here can repair it
    gateway = d.get("gateway") if isinstance(d, dict) else None
    if not isinstance(gateway, str) or not gateway or d.get("binding"):
        return False
    record = record_for(str(d.get("identity") or ""), gateway, str(d.get("owner_dm") or ""))
    if record is None or not write(path, json.dumps(record)):
        return None
    return True


def sweep_legacy_temps(record_path: Path, pid_alive: Callable[[int], bool]) -> int:
    """Unlink `.<record name>.<pid>.tmp` in the record's dir: regular files only (no symlink is
    followed), this uid's, older than the grace, and whose pid is dead or older than the bound."""
    pattern = re.compile(re.escape(f".{record_path.name}.") + r"([0-9]{1,10})\.tmp")
    try:
        dfd = os.open(str(record_path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except FileNotFoundError:
        return 0
    removed = 0
    try:
        for name in os.listdir(dfd):
            m = pattern.fullmatch(name)
            if not m or int(m.group(1)) == os.getpid():
                continue
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            age = time.time() - st.st_mtime
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or age < TEMP_GRACE_S
                    or (age < TEMP_MAX_AGE_S and pid_alive(int(m.group(1))))):
                continue
            os.unlink(name, dir_fd=dfd)
            removed += 1
    finally:
        os.close(dfd)
    return removed
