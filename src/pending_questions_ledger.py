"""The one writer contract for pending-questions.md: every mutation of the file
goes through `update()` — one mkdir lock shared by all writers, a read-transform-
replace under it, and a temp-file + rename that preserves the file's mode. A lock
is removed only by the writer that took it; a held lock makes the write give up
after LOCK_WAIT_SEC, untouched, with the manual remedy in the error.

Writers: pending_questions_store.FileStore (insert, stamp, status), agent-api `/answer` (status
rewrite), engine-conflict-resolve deliver.py (insert above the divider),
auth-preflight-gate.sh (insert, via this module's CLI). Readers keep
`pending_questions_md`; this module only places and replaces text.

CLI: `python3 pending_questions_ledger.py insert <file> [--where top|above-divider]`
with the entry on stdin; exit 1 and the reason on stderr when it did not write.
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
            # Never reclaimed: rmdir names a path, so a stat-then-rmdir can delete
            # a lock a new writer just took. A wedged lock is removed by hand.
            if time.monotonic() >= deadline:
                return (f"could not acquire {lock} within {LOCK_WAIT_SEC}s; the file is "
                        f"untouched. If no writer is running the lock is stale: rmdir '{lock}'")
            time.sleep(0.05)


def under_lock(lock: Path, fn: Callable[[], object]):
    """(error, result): `fn()` run while holding the mkdir lock `lock`; a held
    lock gives up after LOCK_WAIT_SEC with the error and does not run `fn`."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    err = _acquire(lock)
    if err:
        return err, None
    try:
        return None, fn()
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


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


def with_entry(old: str, entry: str, where: str = "top") -> str:
    """`old` with `entry` (ending in a newline) placed at `insert_point`."""
    at = insert_point(old, where)
    head, tail = old[:at], old[at:]
    if head and not head.endswith("\n"):
        head += "\n"
    if where == "top" and at > 0 and not head.endswith("\n\n"):
        head += "\n"
    return head + entry + tail.lstrip("\n") if where == "top" else head + entry + tail


def insert_entry(pq: Path, entry: str, where: str = "top") -> Optional[str]:
    """Insert `entry` (ending in a newline) at `insert_point`."""
    return update(pq, lambda old: with_entry(old, entry, where))


def stamp(pq: Path, token: str, replacement: str) -> Optional[str]:
    """Replace the unique `token` in place; refuses when absent or not unique."""
    def _do(old: str) -> str:
        n = old.count(token)
        if n != 1:
            raise LedgerError(f"token {token!r} occurs {n} times, expected 1")
        return old.replace(token, replacement, 1)
    return update(pq, _do)


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Insert an entry into pending-questions.md.")
    ap.add_argument("op", choices=("insert",))
    ap.add_argument("file", type=Path)
    ap.add_argument("--where", choices=("top", "above-divider"), default="top")
    args = ap.parse_args(argv)
    entry = sys.stdin.read()
    if not entry.strip():
        print("pending_questions_ledger: empty entry on stdin", file=sys.stderr)
        return 1
    err = insert_entry(args.file, entry if entry.endswith("\n") else entry + "\n", args.where)
    if err:
        print(f"pending_questions_ledger: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
