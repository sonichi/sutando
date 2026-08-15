#!/usr/bin/env python3
"""Bounded read of a CLI `--body-file` argument — the single owner of that policy.
Shared by every sender that accepts prose from a file instead of argv.
"""
from __future__ import annotations

import os
import stat as _stat

MAX_BODY_BYTES = 65536


def read_body_file(path):
    """Validate the DESCRIPTOR, never the pathname: a path that stats as regular can
    be swapped for a FIFO before open, and the open then blocks forever."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise SystemExit(f"ERROR: cannot read --body-file {path!r}: {exc}")
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise SystemExit(f"ERROR: --body-file {path!r} is not a regular file "
                             "(a FIFO or device would block or exhaust memory)")
        if st.st_size > MAX_BODY_BYTES:
            raise SystemExit(f"ERROR: --body-file {path!r} is {st.st_size} bytes, "
                             f"over the {MAX_BODY_BYTES} limit")
        raw = b""
        while len(raw) < MAX_BODY_BYTES + 1:
            chunk = os.read(fd, MAX_BODY_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(fd)
    # Size is re-checked from the bytes: the file may grow after fstat.
    if len(raw) > MAX_BODY_BYTES:
        raise SystemExit(f"ERROR: --body-file {path!r} exceeds {MAX_BODY_BYTES} bytes")
    try:
        return raw.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"ERROR: --body-file {path!r} is not UTF-8: {exc}")
