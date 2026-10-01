"""The one writer contract for pending-questions.md: every mutation of the file
goes through `update()` — one mkdir lock shared by all writers, a read-transform-
replace under it, and a temp-file + rename that preserves the file's mode.

Writers: pending_questions_ask (insert + stamp), agent-api `/answer` (status
rewrite), engine-conflict-resolve deliver.py (insert above the divider). Readers
keep `pending_questions_md`; this module only places and replaces text.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from pending_questions_md import DIVIDER_OR_DONE_RE, DIVIDER_RE, mask_markup

LOCK_WAIT_SEC = 10
# A ledger write takes milliseconds; a lock this old belongs to a dead writer.
STALE_LOCK_SEC = 120
NEW_FILE_MODE = 0o644
_TITLE_RE = re.compile(r"^# \S")


class LedgerError(Exception):
    """A transform declining to write (entry not found, token not unique)."""


def lock_path(pq: Path) -> Path:
    return Path(str(pq) + ".lock")


def _acquire(lock: Path) -> Optional[str]:
    deadline = time.monotonic() + LOCK_WAIT_SEC
    while True:
        try:
            lock.mkdir()
            return None
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK_SEC:
                    lock.rmdir()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return f"could not acquire {lock} within {LOCK_WAIT_SEC}s"
            time.sleep(0.05)


def replace_file(pq: Path, text: str) -> None:
    """Appear whole at `pq` in one rename; a crash mid-write leaves the old file."""
    try:
        mode = pq.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = NEW_FILE_MODE
    fd, tmp = tempfile.mkstemp(dir=str(pq.parent), prefix=f".{pq.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, pq)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(pq: Path, transform: Callable[[str], str]) -> Optional[str]:
    """Read, `transform`, replace — under the lock. Returns an error string (lock
    timeout, or the message of a LedgerError the transform raised) or None."""
    pq.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path(pq)
    err = _acquire(lock)
    if err:
        return err
    try:
        old = pq.read_text(encoding="utf-8") if pq.exists() else ""
        try:
            new = transform(old)
        except LedgerError as e:
            return str(e)
        if new != old:
            replace_file(pq, new)
        return None
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def insert_point(text: str, where: str = "top") -> int:
    """Offset for a new entry: `top` = start of the active region, below a `# `
    title line that is not itself the divider; `above-divider` = just before the
    first real divider, else EOF. Both keep the entry out of the archive."""
    if where == "above-divider":
        m = DIVIDER_RE.search(mask_markup(text))
        return m.start() if m else len(text)
    first, nl, _rest = text.partition("\n")
    if nl and _TITLE_RE.match(first) and not DIVIDER_OR_DONE_RE.match(first):
        return len(first) + 1
    return 0


def insert_entry(pq: Path, entry: str, where: str = "top") -> Optional[str]:
    """Insert `entry` (ending in a newline) at `insert_point`."""
    def _do(old: str) -> str:
        at = insert_point(old, where)
        head, tail = old[:at], old[at:]
        if head and not head.endswith("\n"):
            head += "\n"
        if where == "top" and at > 0 and not head.endswith("\n\n"):
            head += "\n"
        return head + entry + tail.lstrip("\n") if where == "top" else head + entry + tail
    return update(pq, _do)


def stamp(pq: Path, token: str, replacement: str) -> Optional[str]:
    """Replace the unique `token` in place; refuses when absent or not unique."""
    def _do(old: str) -> str:
        n = old.count(token)
        if n != 1:
            raise LedgerError(f"token {token!r} occurs {n} times, expected 1")
        return old.replace(token, replacement, 1)
    return update(pq, _do)
