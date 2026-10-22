"""Read a small regular file's text without ever blocking: the one reader for credential files.

A FIFO, device or directory named where a credential file is expected is refused before any read
(opened non-blocking, then fstat), and a file over the size bound is refused too, so startup can
never hang on a path someone pointed at the wrong thing. read_regular says why it refused, so a
caller can name the refusal instead of treating it as absence. Stdlib only; vendored byte-identical
into the ag2-sparrow package by tools/sync_from_src.py.
"""
from __future__ import annotations

import errno
import os
import stat
from typing import Optional, Tuple

MAX_BYTES = 1 << 20
_ABSENT = (errno.ENOENT, errno.ENOTDIR)


def read_regular(path: str, limit: int = MAX_BYTES) -> Tuple[Optional[str], str]:
    """(text, "") when `path` was read; (None, "") when it does not exist; (None, reason) when it
    exists but was refused: not a regular file, over `limit` bytes, unreadable or not UTF-8."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        if e.errno in _ABSENT:
            return None, ""
        return None, f"unreadable ({errno.errorcode.get(e.errno, 'error')})"
    except (ValueError, TypeError):
        return None, "not a usable path"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "not a regular file"
        if info.st_size > limit:
            return None, f"over {limit} bytes"
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:
            return None, f"over {limit} bytes"  # grew past the bound after the fstat
        return b"".join(chunks).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"), ""
    except UnicodeDecodeError:
        return None, "not UTF-8"
    except OSError as e:
        return None, f"unreadable ({errno.errorcode.get(e.errno, 'error')})"
    finally:
        os.close(fd)


def read_regular_text(path: str, limit: int = MAX_BYTES) -> Optional[str]:
    """`path`'s UTF-8 text, or None when it is missing or refused (read_regular says which)."""
    return read_regular(path, limit)[0]
