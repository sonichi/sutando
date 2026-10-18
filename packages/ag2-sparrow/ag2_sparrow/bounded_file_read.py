"""Read a small regular file's text without ever blocking: the one reader for credential files.

A FIFO, device or directory named where a credential file is expected is refused before any read
(opened non-blocking, then fstat), and a file over the size bound is refused too, so startup can
never hang on a path someone pointed at the wrong thing. Stdlib only; vendored byte-identical into
the ag2-sparrow package by tools/sync_from_src.py.
"""
from __future__ import annotations

import os
import stat
from typing import Optional

MAX_BYTES = 1 << 20


def read_regular_text(path: str, limit: int = MAX_BYTES) -> Optional[str]:
    """`path`'s UTF-8 text, or None when it is missing, not a regular file, over `limit` bytes,
    unreadable or not UTF-8."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except (OSError, ValueError, TypeError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:
            return None  # grew past the bound after the fstat
        return b"".join(chunks).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)
