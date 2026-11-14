"""Where sparrowd's control socket lives — the one function every server and client calls.

The natural path is `<state dir>/sparrowd.sock`, the state dir resolved. A Unix socket path is capped by `sun_path`
(104 bytes on macOS, 108 on Linux, counting the terminator), so a long workspace path cannot
bind there. Such a state dir gets a short, deterministic, per-instance path instead:
`/tmp/ag2-sparrow-<uid>/<hash of the state dir>.sock`, inside a 0700 directory this user owns.
A directory that exists but is not ours (or is a symlink, or not a directory) is refused.
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Optional

SOCKET_NAME = "sparrowd.sock"
SUN_PATH_MAX = 104 if sys.platform == "darwin" else 108
# Not $TMPDIR: launchd jobs and shells see different values, and server and client must agree.
SHORT_ROOT = "/tmp"


class ControlSocketPathError(OSError):
    """The short-path directory cannot be trusted; the message says why."""


def fits(path) -> bool:
    return len(os.fsencode(str(path))) + 1 <= SUN_PATH_MAX


def short_dir(uid: Optional[int] = None, root: Optional[str] = None) -> Path:
    return Path(root or SHORT_ROOT) / f"ag2-sparrow-{os.getuid() if uid is None else uid}"


def _short_name(resolved: Path) -> str:
    return hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:16] + ".sock"


def ensure_private_dir(path: Path, uid: Optional[int] = None, create: bool = True) -> None:
    """Create `path` 0700 (with `create`), or accept it only when it is a real directory owned by
    `uid`. Without `create` a missing directory passes: there is nothing there to connect to."""
    uid = os.getuid() if uid is None else uid
    if not create and not os.path.lexists(path):
        return
    try:
        if create:
            os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise ControlSocketPathError(f"{path} is not a directory; refusing to put a control socket there")
    if st.st_uid != uid:
        raise ControlSocketPathError(f"{path} is owned by uid {st.st_uid}, not {uid}; refusing to use it")
    if stat.S_IMODE(st.st_mode) & 0o077:
        if not create:
            raise ControlSocketPathError(f"{path} is open to other users (mode {stat.S_IMODE(st.st_mode):o})")
        os.chmod(path, 0o700)


def control_socket_path(state_dir, *, create: bool = False, uid: Optional[int] = None,
                        root: Optional[str] = None) -> Path:
    """The control socket for `state_dir`. A short path's directory is always owner-checked;
    with `create` (the server) it is made 0700 first, a client only reads."""
    # One resolved spelling decides both the fit and the hash, so every caller agrees.
    resolved = Path(os.path.realpath(os.fspath(state_dir)))
    natural = resolved / SOCKET_NAME
    if fits(natural):
        return natural
    directory = short_dir(uid, root)
    path = directory / _short_name(resolved)
    if not fits(path):
        raise ControlSocketPathError(f"no control socket path fits {SUN_PATH_MAX} bytes under {directory}")
    ensure_private_dir(directory, uid, create)
    return path
