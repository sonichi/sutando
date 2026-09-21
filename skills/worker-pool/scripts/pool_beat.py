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

`--parent-pid` (the watcher's own beat) requires this process to REMAIN the OS
child of the pid it watches: `parent_gone` reads via `getppid()`, so a beat
spawned by anything that itself exits gets reparented and reads "gone" almost
at once. A worker's own beat cannot be born that way — every process this
skill can start from inside a running Claude session is spawned through a
tool call whose shell exits when the call returns, orphaning any background
child to PID 1 (sonichi/sutando#4421 field notes). `--watch-pid` is for that
case: it polls an ARBITRARY pid's liveness by signal, not by parentage, so
the beat writer's own reparenting is irrelevant to what it is reporting on.
"""
from __future__ import annotations

import argparse
import os
import subprocess
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


PARENT_POLL_S = 1.0


def parent_gone(parent_pid: int, *, getppid=os.getppid, kill=os.kill) -> bool:
    """Has the process this beat speaks for died?

    SIGKILL and a crash run no trap, so nobody tells the beat to stop; it has to
    look. Reparenting counts too, or a recycled pid would read as the parent.
    """
    if getppid() != parent_pid:
        return True
    try:
        kill(parent_pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def process_start_time(pid: int, *, run=None) -> str:
    """`ps -o lstart=` for `pid`, verbatim, or `""` when it cannot be read.

    Used only to catch pid reuse: a died-then-recycled pid still answers
    `kill(pid, 0)`, but the NEW process holding it started at a different
    time. `""` (pid gone, or `ps` itself unusable) is never treated as a
    match by the caller — see `target_gone`.
    """
    run = run or subprocess.run
    try:
        r = run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    except OSError:
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def target_gone(pid: int, start_time: str, *, kill=os.kill,
                lstart=process_start_time) -> bool:
    """Has the pid this beat watches died, or been replaced by a DIFFERENT
    process the kernel recycled onto the same number?

    Unlike `parent_gone`, this asks about an arbitrary pid rather than our
    own OS parent — the only question that still means something once the
    beat writer itself has been reparented to PID 1, which it always will be.
    `start_time` empty (never recorded) skips the reuse check, matching the
    very first call before any start time has been captured.
    """
    try:
        kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass  # it exists; we may just not be allowed to signal it
    current = lstart(pid)
    if start_time and current and current != start_time:
        return True
    return False


def run_forever(path, interval: float = BEAT_INTERVAL_S, *, parent_pid=None,
                watch_pid=None, poll_s: float = PARENT_POLL_S) -> int:
    watch_start = ""
    if watch_pid is not None:
        if target_gone(watch_pid, ""):
            return 0
        watch_start = process_start_time(watch_pid)
    if parent_pid is not None and parent_gone(parent_pid):
        return 0
    touch(path)
    while True:
        if parent_pid is None and watch_pid is None:
            time.sleep(interval)
        else:
            # Polled far more often than the beat is refreshed: a dead target
            # must not get one more fresh mtime out of a 30 s sleep.
            waited = 0.0
            while waited < interval:
                step = min(poll_s, interval - waited)
                time.sleep(step)
                waited += step
                if parent_pid is not None and parent_gone(parent_pid):
                    return 0
                if watch_pid is not None and target_gone(watch_pid, watch_start):
                    return 0
        touch(path)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", required=True)
    p.add_argument("--kind", required=True, choices=sorted(KINDS))
    p.add_argument("--id", required=True, dest="ident")
    p.add_argument("--interval", type=float, default=BEAT_INTERVAL_S)
    p.add_argument("--parent-pid", type=int, default=None,
                   help="exit once this pid is no longer our parent (it died)")
    p.add_argument("--watch-pid", type=int, default=None,
                   help="exit once this pid is gone or recycled, by signal "
                        "not by parentage — for a beat that cannot be born "
                        "as that pid's direct OS child")
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
    # Conditional: pool-beat.test.py's existing spy takes no watch_pid kwarg,
    # and passing it unconditionally would break a suite this touches nothing.
    kwargs = {"parent_pid": a.parent_pid}
    if a.watch_pid is not None:
        kwargs["watch_pid"] = a.watch_pid
    return run_forever(path, a.interval, **kwargs)


if __name__ == "__main__":
    sys.exit(main())
