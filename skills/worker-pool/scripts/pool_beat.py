#!/usr/bin/env python3
"""Worker and watcher beats: `state/workers/<id>.alive`, `state/watchers/<id>.alive`.

The roster carries a worker `state` that every consumer honours and nothing has
ever written, because the signal that is supposed to set `live` does not exist
(sonichi/sutando#4105). This is that signal, to the spec in
`docs/worker-pool-design.md`: **the beat is an mtime**, refreshed every 30 s and
considered stale at 90 s, and **a future-dated beat counts as stale too**.

Mtime only. Nothing reads the contents, so nothing may come to depend on them —
a payload here is how the next reader starts keying on a field instead of the
clock. Writing is a truncate-free `utime`, so a beat never competes with a
reader for bytes that do not exist.

Three states, never two: `absent` is not `stale`. A host that has never run a
beat writer and a worker that died look identical to a two-valued check, and
reading the first as death abandons every worker on the release that introduces
this file.

    beat_path(workspace, "worker", wid)   -> Path
    touch(path)                            -> None
    classify(path, now, stale_s=90)        -> "live"|"stale"|"absent"|"unknown"
    run_forever(path, interval=30)         -> never returns
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

#: From the design. A host sleep expires every beat at once, which is why a
#: release is never authorised by staleness alone.
BEAT_INTERVAL_S = 30.0
STALE_AFTER_S = 90.0

KINDS = {"worker": "workers", "watcher": "watchers"}

LIVE = "live"
STALE = "stale"
ABSENT = "absent"
#: Unreadable. NOT stale: `stale` is the value a reaper acts on, and an EACCES
#: on one beat file must never read as "this worker died".
UNKNOWN = "unknown"


def beat_path(workspace, kind: str, ident: str) -> Path:
    """`<workspace>/state/<workers|watchers>/<id>.alive`."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}, not {kind!r}")
    if not ident or "/" in ident or ident in (".", ".."):
        raise ValueError(f"not a usable id: {ident!r}")
    return Path(workspace) / "state" / KINDS[kind] / f"{ident}.alive"


def touch(path) -> None:
    """Refresh the mtime, creating the file empty. Never writes bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.utime(fd, None)
    finally:
        os.close(fd)


def classify(path, now: float, *, stale_s: float = STALE_AFTER_S) -> str:
    """live / stale / absent / unknown. A future mtime is a clock fault.

    `unknown` is unreadable, and is NOT folded into `stale` — a caller that
    reaps on `stale` would otherwise reap a live worker on one EACCES.
    """
    try:
        mtime = Path(path).stat().st_mtime
    except (FileNotFoundError, NotADirectoryError):
        return ABSENT
    except OSError:
        return UNKNOWN
    age = now - mtime
    if age < 0:
        return STALE
    return LIVE if age <= stale_s else STALE


def run_forever(path, interval: float = BEAT_INTERVAL_S) -> int:
    touch(path)
    while True:
        time.sleep(interval)
        touch(path)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", required=True)
    p.add_argument("--kind", required=True, choices=sorted(KINDS))
    p.add_argument("--id", required=True, dest="ident")
    p.add_argument("--interval", type=float, default=BEAT_INTERVAL_S)
    p.add_argument("--once", action="store_true", help="write one beat and exit")
    p.add_argument("--read", action="store_true", help="print this beat's state and exit")
    a = p.parse_args(argv)
    path = beat_path(a.workspace, a.kind, a.ident)
    if a.read:
        print(classify(path, time.time()))
        return 0
    if a.once:
        touch(path)
        return 0
    return run_forever(path, a.interval)


if __name__ == "__main__":
    sys.exit(main())
