#!/usr/bin/env python3
"""Serialized, generation-checked filesystem transitions for notifier leases."""
import fcntl
import os
from pathlib import Path
import stat
import sys
import tempfile


def snapshot(directory):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("lease is not a directory")
    values = [f"{info.st_dev}:{info.st_ino}:{info.st_ctime_ns}"]
    for name in ("token", "pid"):
        path = directory / name
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("lease metadata is not a regular file")
            value = path.read_text().rstrip("\n")
        except FileNotFoundError:
            value = ""
        if "|" in value or "\n" in value:
            raise ValueError("invalid lease metadata")
        values.append(value)
    return "|".join(values)


def transition(guard, action, directory, expected):
    # This inode is permanent: unlinking it would split waiting contenders
    # across independent locks. The kernel releases flock even after SIGKILL.
    fd = os.open(guard, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("guard is not a regular file")
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = snapshot(directory)
        if action == "snapshot":
            print(current)
            return True
        if action == "publish":
            if current.split("|")[1]:
                return False
            stage_fd, stage = tempfile.mkstemp(prefix=directory.name + ".stage.", dir=directory.parent)
            try:
                with os.fdopen(stage_fd, "w") as out:
                    out.write(expected)
                os.link(stage, directory / "token")
                (directory / "pid").write_text(expected.split(":", 1)[0] + "\n")
            finally:
                os.unlink(stage)
            return True
        if action != "remove" or current != expected:
            return False
        for path in [directory / "token", directory / "pid", *directory.glob(".token.*")]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        directory.rmdir()
        return True


if __name__ == "__main__":
    try:
        ok = transition(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4])
    except (OSError, ValueError):
        ok = False
    sys.exit(0 if ok else 1)
