#!/usr/bin/env python3
"""Naming and moves for `results/undelivered/` — the delivery quarantine.

The convention (`<stem>-<epoch>.txt`) had exactly one owner, the sparrow bridge,
and only one direction: in. Operator recovery needs the way back, and a second
copy of the name format in a second module is how the two drift. So the format
lives here once and both directions are derived from it.

Every move into or out of the quarantine goes through ONE no-replace transition
(`rename_noreplace`), and the two directions each keep an explicit guarantee:

* into it (`place`): a quarantined copy is never overwritten — the target name is
  allocated fresh and a taken one is skipped, so earlier evidence always survives;
* out of it (`restore`): the exact quarantined body is installed at the live name
  in one atomic step, or it stays quarantined and the refusal is reported — never
  a link-then-unlink a producer could interleave with.

Dependency-light on purpose: no bridge import, no gateway, no env. The caller
supplies the results directory.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import re
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

DIRNAME = "undelivered"
# `<task stem>-<unix seconds>.txt`; the stem may itself contain hyphens.
_QUARANTINED = re.compile(r"^(?P<stem>.+)-(?P<epoch>\d+)\.txt$")


def quarantine_dir(results_dir: Path) -> Path:
    return Path(results_dir) / DIRNAME


def quarantine_name(stem: str, when: Optional[int] = None) -> str:
    """The one place the quarantined filename is spelled. Nanoseconds, not
    seconds: the drain can quarantine one task several times inside a second."""
    return f"{stem}-{int(when if when is not None else time.time_ns())}.txt"


# ── the one no-replace transition ────────────────────────────────────────────

Log = Callable[[str], None]
# Reported once per process; the lifecycle owner shares this set.
_REPORTED: "set[str]" = set()


def report_once(key: str, log: Log, line: str) -> None:
    """Say `line` once per process for `key`."""
    if key not in _REPORTED:
        _REPORTED.add(key)
        log(line)


def _probe_rename() -> "tuple[str, Optional[Callable[[bytes, bytes], int]]]":
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    except OSError:  # pragma: no cover - no libc to speak of
        return "none", None
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):  # pragma: no cover - Linux CI
        fn = libc.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        return "renamex_np", lambda a, b: fn(a, b, 4)                    # RENAME_EXCL
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        at_fdcwd = -100
        return "renameat2", lambda a, b: fn(at_fdcwd, a, at_fdcwd, b, 1)  # RENAME_NOREPLACE
    if sys.platform == "win32":  # pragma: no cover - os.rename refuses a taken name there
        return "os.rename", None
    return "none", None


RENAME_PRIMITIVE, _RENAME = _probe_rename()


def rename_noreplace(src: Path, dst: Path, log: Optional[Log] = None) -> None:
    """Rename `src` to `dst` only if `dst` does not exist: FileExistsError
    (EEXIST) otherwise, and nothing moved. Without a kernel primitive nothing
    is moved either and FileExistsError carries ENOTSUP: a link-then-unlink
    would have to unlink `src` after a check another writer can invalidate."""
    global RENAME_PRIMITIVE, _RENAME
    if RENAME_PRIMITIVE == "os.rename":  # pragma: no cover - Windows only
        os.rename(src, dst)
        return
    if _RENAME is not None:
        rc = _RENAME(os.fsencode(src), os.fsencode(dst))
        if rc == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(dst))
        if err in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
            RENAME_PRIMITIVE, _RENAME = "none", None   # this filesystem cannot; refuse from now on
        else:
            raise OSError(err, os.strerror(err), str(src))
    report_once("rename:fallback", log or (lambda _m: None),
                "result disposal: no no-replace rename on this platform; a reply that would go "
                "back to its name is kept in undelivered/ instead, never put back")
    raise FileExistsError(errno.ENOTSUP, "no no-replace rename on this platform; nothing moved", str(dst))


def no_primitive(e: OSError) -> bool:
    """The refusal meant "this platform cannot", not "the name is taken"."""
    return isinstance(e, FileExistsError) and e.errno == errno.ENOTSUP


# Enough to step past any burst of same-nanosecond copies of one task.
_PLACE_TRIES = 64


def place(src: Path, results_dir: Path, stem: str,
          move: Optional[Callable[[Path, Path], None]] = None,
          when: Optional[int] = None) -> Path:
    """Guarantee (into the quarantine): move `src` there under a name nothing
    holds yet, never replacing a file. `move` must refuse a taken target with
    FileExistsError(EEXIST); a taken name is skipped for the next one, so an
    earlier quarantined copy always survives. Returns the new path."""
    d = quarantine_dir(results_dir)
    d.mkdir(parents=True, exist_ok=True)
    move = move or rename_noreplace
    n = int(when if when is not None else time.time_ns())
    for _ in range(_PLACE_TRIES):
        target = d / quarantine_name(stem, n)
        try:
            move(Path(src), target)
            return target
        except FileExistsError as e:
            if e.errno not in (None, errno.EEXIST):
                raise
            n += 1
    raise FileExistsError(errno.EEXIST, "no free quarantine name", str(d))


def quarantine(rfile: Path, results_dir: Path,
               when: Optional[int] = None) -> Path:
    """Move a refused result out of the drain's view, never over an earlier
    quarantined copy. Returns the new path."""
    return place(Path(rfile), results_dir, Path(rfile).stem, when=when)


def find_quarantined(results_dir: Path, task_id: str) -> list[Path]:
    """Every quarantined copy of one task, oldest first.

    A task can be quarantined more than once (each failed recovery adds one), so
    the caller decides which to restore rather than being handed a guess.
    """
    d = quarantine_dir(results_dir)
    if not d.is_dir():
        return []
    stem = f"task-{task_id}" if not str(task_id).startswith("task-") else str(task_id)
    out = []
    for p in d.glob("*.txt"):
        m = _QUARANTINED.match(p.name)
        if m and m.group("stem") == stem:
            out.append((int(m.group("epoch")), p))
    return [p for _, p in sorted(out)]


class RestoreOutcome(str, Enum):
    """Absence and refusal are different answers: one means the body is gone,
    the other that a newer reply is already queued. Collapsing them to None
    leaves the operator unable to tell whether they still have a problem."""

    RESTORED = "restored"
    NOTHING_QUARANTINED = "nothing-quarantined"
    LIVE_RESULT_PRESENT = "live-result-present"
    # No atomic no-replace move on this platform: the body stays quarantined.
    NO_SAFE_MOVE = "no-safe-move"


def restore(results_dir: Path, task_id: str) -> "tuple[RestoreOutcome, Optional[Path]]":
    """Guarantee (out of the quarantine): the NEWEST quarantined body is
    installed at the drain's canonical name in one no-replace rename, or it
    stays quarantined and the outcome says why. A live result is never
    overwritten (a newer reply waiting to go is the one the user should get),
    and the body is never left without a name: there is no link-then-unlink
    for a producer retaking the name to interleave with.
    """
    found = find_quarantined(results_dir, task_id)
    if not found:
        return RestoreOutcome.NOTHING_QUARANTINED, None
    stem = f"task-{task_id}" if not str(task_id).startswith("task-") else str(task_id)
    target = Path(results_dir) / f"{stem}.txt"
    try:
        rename_noreplace(found[-1], target)
    except FileExistsError as e:
        if no_primitive(e):
            return RestoreOutcome.NO_SAFE_MOVE, found[-1]
        return RestoreOutcome.LIVE_RESULT_PRESENT, target
    return RestoreOutcome.RESTORED, target
