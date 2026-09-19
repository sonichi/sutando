#!/usr/bin/env python3
"""Append one line to an optional diagnostic log without ever blocking the caller.

A shell `>>` blocks forever when the path is a FIFO nobody reads, and `|| true`
cannot rescue an open(2) that never returns. Here the open is O_NONBLOCK — a
reader-less FIFO answers ENXIO at once — and the descriptor is then fstat'ed, so
only a REGULAR file is ever written. A FIFO (with or without a reader), a
device, a directory or an unwritable path is skipped and the caller goes on:
the log is optional, the caller's startup is not.

CLI: diagnostic_append.py <path> <line>   — exit 0 whether or not the line
landed (the caller must not fail on it); exit 2 only on usage.
"""
from __future__ import annotations

import os
import stat
import sys


def append_line(path: str, line: str) -> bool:
    """True when `line` (newline-terminated) was written to a regular file."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        data = (line.rstrip("\n") + "\n").encode("utf-8", "replace")
        while data:
            data = data[os.write(fd, data):]
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: diagnostic_append.py <path> <line>", file=sys.stderr)
        sys.exit(2)
    append_line(sys.argv[1], sys.argv[2])
    sys.exit(0)
