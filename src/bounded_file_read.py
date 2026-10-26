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


def read_regular(path: str, limit: int = MAX_BYTES, private: bool = False) -> Tuple[Optional[str], str]:
    """(text, "") when `path` was read; (None, "") when it does not exist; (None, reason) when it
    exists but was refused: not a regular file, over `limit` bytes, unreadable or not UTF-8. `private`
    also refuses a symlink, a file another user owns, and any group or other permission bit."""
    private = private and os.name == "posix"  # elsewhere st_mode and st_uid carry no ownership to check
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | (os.O_NOFOLLOW if private else 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno in _ABSENT:
            return None, ""
        if private and e.errno == errno.ELOOP:
            return None, "a symlink; a private credential file must be the file itself"
        return None, f"unreadable ({errno.errorcode.get(e.errno, 'error')})"
    except (ValueError, TypeError):
        return None, "not a usable path"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "not a regular file"
        if private and info.st_uid != os.geteuid():
            return None, f"owned by uid {info.st_uid}, not this user"
        if private and info.st_mode & 0o077:
            return None, f"mode {stat.S_IMODE(info.st_mode):04o}; a private credential file must be 0600"
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


def read_private(path: str, limit: int = MAX_BYTES) -> Tuple[Optional[str], str]:
    """read_regular for a file that holds a credential by itself: this user's, mode 0600, no symlink."""
    return read_regular(path, limit, private=True)


def read_regular_text(path: str, limit: int = MAX_BYTES) -> Optional[str]:
    """`path`'s UTF-8 text, or None when it is missing or refused (read_regular says which)."""
    return read_regular(path, limit)[0]
