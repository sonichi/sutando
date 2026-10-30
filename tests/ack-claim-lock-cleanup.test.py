#!/usr/bin/env python3
"""Orphaned per-key lock files in state/ack-claims/ are bounded, and cleanup never races a publisher.

  a) a released key's lock, past retention and unheld, is removed
  b) a lock whose record still exists, or that is younger than retention, stays
  c) a lock another publisher holds survives
  d) a publisher that has opened the lock but not yet acquired it: cleanup waits for it, never
     unlinks the inode under it, and a later sweep removes the lock after the publisher is done
     CONTROL (d): without the directory lock, cleanup unlinks during that window and the
     publisher's inode check has to reopen
  e) the directory lock itself is never removed

Run: python3 tests/ack-claim-lock-cleanup.test.py
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import ack_claim  # noqa: E402

FAILS = []
KEY = "ack:task-1761300000001:initial"
RET = 3600.0


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def released_lock(ws: Path, key: str = KEY, age: float = 2 * RET) -> Path:
    gen = ack_claim.claim(ws, key, "status-ack")
    ack_claim.update(ws, key, "status-ack", gen=gen, via="x")
    ack_claim.release(ws, key, "status-ack", gen=gen)
    lock = ack_claim.claims_dir(ws) / f"{key.replace(':', '+')}.lock"
    t = os.stat(lock).st_mtime - age
    os.utime(lock, (t, t))
    return lock


def ws_() -> Path:
    return Path(tempfile.mkdtemp(prefix="ack-lock-cleanup-"))


def paused_publisher(ws: Path, opened: threading.Event, go: threading.Event, calls: list):
    def hook(key):
        calls.append(os.stat(ack_claim.claims_dir(ws) / f"{key.replace(':', '+')}.lock").st_ino)
        if len(calls) == 1:
            opened.set()
            go.wait(10)

    def run():
        ack_claim._after_open = hook
        try:
            ack_claim.update(ws, KEY, "status-ack", via="late")
        finally:
            ack_claim._after_open = None

    return threading.Thread(target=run)


def interleave(ws: Path, lock: Path):
    """Pause a publisher between open and acquire, run cleanup, report what happened."""
    opened, go, calls, done = threading.Event(), threading.Event(), [], []
    pub = paused_publisher(ws, opened, go, calls)
    pub.start()
    opened.wait(10)
    ino = os.stat(lock).st_ino
    cleaner = threading.Thread(target=lambda: done.append(ack_claim.prune_orphan_locks(ws, os.stat(lock).st_mtime + 2 * RET, RET)))
    cleaner.start()
    cleaner.join(0.5)
    during = (bool(done), lock.exists() and os.stat(lock).st_ino == ino)
    go.set()
    pub.join(10)
    cleaner.join(10)
    assert not pub.is_alive() and not cleaner.is_alive(), "publisher or cleaner did not finish"
    return during, done, calls


def main() -> int:
    ws = ws_()
    lock = released_lock(ws)
    now = os.stat(lock).st_mtime + 2 * RET
    removed = ack_claim.prune(ws, lambda rec: False, now, RET)
    check(removed == [f"lock:{lock.stem}"] and not lock.exists(),
          f"a) prune removes an old, unheld lock of a released key ({removed})")

    ws = ws_()
    young = released_lock(ws, age=0)
    kept = ack_claim.claim(ws, "ack:task-1761300000002:initial", "status-ack")
    ack_claim.update(ws, "ack:task-1761300000002:initial", "status-ack", gen=kept, via="x")
    live = ack_claim.claims_dir(ws) / "ack+task-1761300000002+initial.lock"
    os.utime(live, (0, 0))
    check(ack_claim.prune_orphan_locks(ws, os.stat(young).st_mtime + 10, RET) == [] and young.exists(),
          "b) a lock younger than retention stays")
    check(ack_claim.prune_orphan_locks(ws, 10 * RET, RET) == [] and live.exists(),
          "b) a lock whose record still exists stays")

    ws = ws_()
    lock = released_lock(ws)
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        removed = ack_claim.prune_orphan_locks(ws, os.stat(lock).st_mtime + 2 * RET, RET)
    check(removed == [] and lock.exists(), "c) a lock another publisher holds survives cleanup")

    ws = ws_()
    lock = released_lock(ws)
    now = os.stat(lock).st_mtime + 2 * RET
    (waited, intact), done, calls = interleave(ws, lock)
    check(not waited and intact, "d) cleanup waits while a publisher is between open and acquire; the inode stays")
    removed = ack_claim.prune_orphan_locks(ws, now, RET)
    check(done in ([[]], [[f"lock:{lock.stem}"]]) and done[0] + removed == [f"lock:{lock.stem}"]
          and not lock.exists() and len(calls) == 1,
          f"d) a post-publisher sweep removes any lock skipped while held ({done}, {removed}, {len(calls)} open(s))")

    ws = ws_()
    lock = released_lock(ws)
    real = ack_claim._dir_lock
    ack_claim._dir_lock = lambda d, mode: contextlib.nullcontext()
    try:
        (waited, intact), done, calls = interleave(ws, lock)
    finally:
        ack_claim._dir_lock = real
    check(waited and not intact and len(calls) == 2,
          f"CONTROL (d): without the directory lock cleanup unlinks mid-open and the publisher reopens ({len(calls)})")

    check((ack_claim.claims_dir(ws) / ack_claim.DIR_LOCK).exists(), "e) the directory lock is never removed")
    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
