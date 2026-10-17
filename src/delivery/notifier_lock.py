#!/usr/bin/env python3
"""Run one command holding an exclusive kernel lock, so at most one copy runs per lock file.

    notifier_lock.py --lock PATH [--timeout S] [--log-every S] [--log-file F] -- CMD [ARGS...]
    notifier_lock.py --lock PATH --check-held FD    # exit 0 only if inherited FD holds PATH's lock

The lock is fcntl.flock(LOCK_EX) on PATH, moved to fd 9 and inherited across exec, with
SUTANDO_NOTIFIER_LOCK_FD=9 in the environment. It is held by CMD and by every descendant that still
has fd 9 open, so it lasts until the last of them exits, kill -9 included; a successor cannot run
while any process that could still act for CMD is alive.
A second caller waits, logging every --log-every seconds (to stderr and --log-file), and exits 75
after --timeout. While it waits, PATH.waiting holds "<pid> <epoch it began waiting>", for health-check.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time

LOCK_FD = 9
EXIT_BUSY = 75
POLL_S = 0.1


def stderr_and_file(log_file: "str | None"):
    def log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)
        if log_file and os.path.isdir(os.path.dirname(log_file) or "."):
            try:
                with open(log_file, "a") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")
            except OSError:
                pass
    return log


def acquire(path: str, timeout: float, log_every: float, *, clock=time.monotonic, sleep=time.sleep,
            log=stderr_and_file(None)) -> "int | None":
    """An fd holding LOCK_EX on `path`, or None when another holder kept it past `timeout`."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    waiting = path + ".waiting"
    start = clock()
    next_log = start + log_every
    announced = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if _same_file(fd, path):
                    return fd
                # The path was replaced while we waited: a lock on the old file guards nothing.
                os.close(fd)
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                continue
            except BlockingIOError:
                now = clock()
                if not announced:
                    _write_waiting(waiting)
                    announced = True
                if now - start >= timeout:
                    os.close(fd)
                    return None
                if now >= next_log:
                    log(f"notifier-lock: waiting {now - start:.0f}s for {path}; another notifier holds it")
                    next_log = now + log_every
                sleep(POLL_S)
    finally:
        try:
            with open(waiting) as fh:
                mine = fh.read().split()[:1] == [str(os.getpid())]
            if mine:
                os.unlink(waiting)
        except OSError:
            pass


def _same_file(fd: int, path: str) -> bool:
    try:
        a, b = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _write_waiting(waiting: str) -> None:
    try:
        with open(waiting, "w") as fh:
            fh.write(f"{os.getpid()} {int(time.time())}\n")
    except OSError:
        pass


def holds(fd: int, path: str) -> bool:
    """`fd` is open on `path` and its open file description holds the exclusive lock."""
    if not _same_file(fd, path):
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def main(argv: "list[str] | None" = None, *, execvpe=os.execvpe) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check-held" in argv and "--" not in argv:
        ap = argparse.ArgumentParser()
        ap.add_argument("--lock", required=True)
        ap.add_argument("--check-held", type=int, required=True)
        args = ap.parse_args(argv)
        return 0 if holds(args.check_held, args.lock) else 1
    if "--" not in argv:
        print(__doc__, file=sys.stderr)
        return 2
    split = argv.index("--")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lock", required=True)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--log-every", type=float, default=30.0)
    ap.add_argument("--log-file")
    args = ap.parse_args(argv[:split])
    cmd = argv[split + 1:]
    if not cmd:
        print(__doc__, file=sys.stderr)
        return 2
    log = stderr_and_file(args.log_file)
    fd = acquire(args.lock, args.timeout, args.log_every, log=log)
    if fd is None:
        log(f"notifier-lock: {args.lock} still held after {args.timeout:.0f}s; not starting")
        return EXIT_BUSY
    if fd != LOCK_FD:
        os.dup2(fd, LOCK_FD)
        os.close(fd)
    os.set_inheritable(LOCK_FD, True)
    execvpe(cmd[0], cmd, dict(os.environ, SUTANDO_NOTIFIER_LOCK_FD=str(LOCK_FD)))
    return 0  # only reached with a test's execvpe


if __name__ == "__main__":
    sys.exit(main())
