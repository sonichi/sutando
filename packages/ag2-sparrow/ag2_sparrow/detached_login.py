"""The process side of a sign-in that outlives the call that started it: a CLI's own sign-in run by a
small detached supervisor, recorded by pid AND start time so that only that very process is ever
signalled, and one lock over its state dir. Claude Code's two-step sign-in and Codex's device-code
sign-in both keep their state this way; what each CLI prints and is given stays in its adapter.

Nothing here reads what a sign-in prints. Stdlib only.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

# A terminal's colour and link sequences, removed before a CLI's words are read.
ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]")


def born(pid) -> str:
    """When the process started, as `ps` tells it; '' when there is none. With the pid, its identity."""
    try:
        r = subprocess.run(["/bin/ps", "-p", str(int(pid)), "-o", "lstart="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return ""
    return " ".join(r.stdout.split()) if r.returncode == 0 else ""


def ours(entry) -> int:
    """The pid of a recorded [pid, born] process that is still that very process, else 0."""
    try:
        pid, was = int(entry[0]), str(entry[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return 0
    return pid if was and pid != os.getpid() and born(pid) == was else 0


def end(entry) -> bool:
    """SIGTERM the recorded process when it is still that very process (pid and start time); whether it was."""
    pid = ours(entry)
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def _running(entry) -> bool:
    """Whether a recorded [pid, born] process is still that very process and not merely awaiting its reap."""
    pid = ours(entry)
    if not pid:
        return False
    try:
        r = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True
    return "Z" not in r.stdout


def descendants(pid) -> list:
    """[pid, born] of every live descendant of `pid`, read from `ps`; each is signalled later only while it is
    still that very process."""
    try:
        r = subprocess.run(["/bin/ps", "-A", "-o", "pid=", "-o", "ppid="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    kids = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, todo = [], list(kids.get(int(pid), []))
    while todo:
        child = todo.pop()
        found.append([child, born(child)])
        todo.extend(kids.get(child, []))
    return [e for e in found if e[1]]


def stop_all(entries, wait_s: float, sleep=time.sleep) -> list:
    """End each recorded process: SIGTERM, up to `wait_s` for it to go, then SIGKILL; each signal goes to the
    exact pid only after its start time is checked again. The entries still running after that."""
    entries = [e for e in entries if e]
    for e in entries:
        end(e)
    deadline = time.monotonic() + wait_s
    while any(_running(e) for e in entries) and time.monotonic() < deadline:
        sleep(0.05)
    for e in entries:
        pid = ours(e) if _running(e) else 0
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    deadline = time.monotonic() + wait_s
    while any(_running(e) for e in entries) and time.monotonic() < deadline:
        sleep(0.05)
    return [e for e in entries if _running(e)]


@contextmanager
def locked(state_dir):
    """One at a time over a sign-in's state: a stable lock file beside the dir, never removed."""
    d = Path(state_dir)
    d.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d.parent / (d.name + ".lock")), os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_private(path: Path, data: dict) -> None:
    """`data` as JSON at `path`, owner-only, replaced in one step."""
    tmp = str(path) + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, str(path))
