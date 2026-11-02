#!/usr/bin/env python3
"""The one-supervisor-per-inbox slot: a holder_lock on the inbox directory, held for a supervisor's life.

    notifier_slot.py hold --inbox DIR --parent PID [--wait SECONDS]
    notifier_slot.py serve --inbox DIR -- COMMAND [ARGS...]

Run by task-notifier-supervisor.sh in the background, this process takes the lock on the inbox's
canonical directory, so two spellings of one inbox (a trailing slash, a symlink) share one slot. It
waits up to --wait seconds for a predecessor that is still exiting. Its first stdout line is the
verdict, then it stays silent:

    held                     this supervisor owns the inbox
    busy <pid> <start>       another holder's record names that live process
    unknown <reason>         the slot could not be decided (unreadable lock, unsupported platform)

While held it watches the parent (pid and start time): once the parent is gone, or on TERM/INT, it
releases the lock and exits (TERM, INT and HUP alike). The flock decides; nothing here signals another process.

`serve` binds exclusion to the notifier's own lifetime, not the helper's: it takes the inbox's serving
flock without waiting and execs COMMAND as a new session leader that inherits the locked fd, so the lock
lasts exactly as long as that process tree (its watcher too). Exit 3: another notifier still serves the
inbox; exit 4: the serving lock could not be read. Either way nothing is started.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import holder_lock  # noqa: E402

ADAPTER = "task-notifier-supervisor"
SERVING = "serving"
PARENT_POLL_S = 1.0


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _holder_of(prior) -> str:
    rec = prior.record if isinstance(prior, holder_lock.Valid) else {}
    return f"{rec.get('pid', '?')} {rec.get('start', '?')}"


def _parent_alive(pid: int, start: "str | None", check_start: bool) -> bool:
    """kill -0 on every poll; the start time (one ps) only when `check_start`, to catch a reused pid."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return not check_start or holder_lock.verify_lineage(pid, start) is not False


def hold(inbox: str, parent: int, wait_s: float, poll=PARENT_POLL_S) -> int:
    root = os.path.realpath(inbox)
    parent_start = holder_lock.start_time(parent)
    try:
        result = holder_lock.acquire(root, install_id=holder_lock.install_id(), timeout=wait_s)
    except (holder_lock.LockError, OSError, RuntimeError) as e:
        _say(f"unknown {type(e).__name__}: {e}")
        return 4
    if isinstance(result, (holder_lock.Held, holder_lock.Contended, holder_lock.TimedOut)):
        _say(f"busy {_holder_of(result.holder)}")
        return 3
    if not isinstance(result, holder_lock.Acquired):
        _say(f"unknown {type(result).__name__}")
        return 4
    owner = result.owner
    stop = {"now": False}

    def on_signal(signum, frame):
        stop["now"] = True
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)
    try:
        holder_lock.publish(owner, ADAPTER)
        _say("held")
        n = 0
        while not stop["now"] and _parent_alive(parent, parent_start, n % 5 == 0):
            time.sleep(poll)
            n += 1
    finally:
        try:
            holder_lock.release(owner)
        except holder_lock.LockError:
            pass
    return 0


def serve(inbox: str, argv: "list[str]", execv=os.execv, setsid=os.setsid) -> int:
    """Exec `argv` holding the inbox's serving lock; refuse (3 held, 4 unreadable) and start nothing otherwise."""
    flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        dir_fd = os.open(holder_lock.lock_dir(os.path.realpath(inbox)), os.O_RDONLY | flags)
        try:
            fd = os.open(SERVING, os.O_RDWR | os.O_CREAT | flags, 0o600, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, holder_lock.LockError) as e:
        print(f"notifier_slot: the serving lock for {inbox} is unreadable ({type(e).__name__}: {e}); "
              "not starting a notifier", file=sys.stderr)
        return 4
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        print(f"notifier_slot: a notifier for {inbox} is still serving; not starting another", file=sys.stderr)
        return 3
    os.set_inheritable(fd, True)
    setsid()
    execv(argv[0], argv)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hold")
    h.add_argument("--inbox", required=True)
    h.add_argument("--parent", type=int, required=True)
    h.add_argument("--wait", type=float, default=15.0)
    v = sub.add_parser("serve")
    v.add_argument("--inbox", required=True)
    v.add_argument("command", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        command = a.command[1:] if a.command[:1] == ["--"] else a.command
        if not command:
            ap.error("serve needs a command after --")
        return serve(a.inbox, command)
    return hold(a.inbox, a.parent, a.wait)


if __name__ == "__main__":
    sys.exit(main())
