#!/usr/bin/env python3
"""Bounded read of a CLI `--body-file` argument — the single owner of that policy.

Every sender that accepts prose from a file instead of argv shares these bounds.
A second copy would let one send path keep a blocking or unbounded read after the
other was fixed.
"""
from __future__ import annotations

import os
import stat as _stat

MAX_BODY_BYTES = 65536


def read_body_file(path):
    """Bounded read of a REGULAR file: a FIFO blocks forever and a device or
    huge file exhausts memory, so neither may reach a whole-file read."""
    try:
        st = os.stat(path)
    except OSError as exc:
        raise SystemExit(f"ERROR: cannot read --body-file {path!r}: {exc}")
    if not _stat.S_ISREG(st.st_mode):
        raise SystemExit(f"ERROR: --body-file {path!r} is not a regular file "
                         "(a FIFO or device would block or exhaust memory)")
    if st.st_size > MAX_BODY_BYTES:
        raise SystemExit(f"ERROR: --body-file {path!r} is {st.st_size} bytes, "
                         f"over the {MAX_BODY_BYTES} limit")
    with open(path, "rb") as fh:
        raw = fh.read(MAX_BODY_BYTES + 1)
    # Re-check after reading: the file may have grown since the stat above.
    if len(raw) > MAX_BODY_BYTES:
        raise SystemExit(f"ERROR: --body-file {path!r} exceeds {MAX_BODY_BYTES} bytes")
    try:
        return raw.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"ERROR: --body-file {path!r} is not UTF-8: {exc}")
